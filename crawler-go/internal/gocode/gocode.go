// Package gocode extracts L1 of the graph for a Go repository: packages, files,
// symbols and the links between them (CALLS, IMPLEMENTS, REFERENCES, TESTS).
package gocode

import (
	"go/ast"
	"go/constant"
	"go/token"
	"go/types"
	"path/filepath"
	"reflect"
	"strconv"
	"strings"

	"golang.org/x/tools/go/packages"

	"graphify/crawler-go/internal/goprog"
	"graphify/crawler-go/internal/model"
)

type symInfo struct {
	key      string
	name     string
	relPkg   string
	kind     string
	file     string
	line     int
	testFile bool
	obj      types.Object
}

type testCall struct {
	from *symInfo
	to   string
	file string
	line int
}

type extractor struct {
	prog      *goprog.Program
	src       model.Source
	c         *model.Claims
	symbols   map[string]*symInfo
	byPkgName map[string][]*symInfo
	testCalls []testCall
	named     []*types.TypeName
	ifaces    []*types.TypeName
	seenFiles map[string]bool
}

// Extract builds the code claims of a loaded program.
func Extract(prog *goprog.Program, src model.Source) *model.Claims {
	x := &extractor{
		prog:      prog,
		src:       src,
		c:         model.New(),
		symbols:   map[string]*symInfo{},
		byPkgName: map[string][]*symInfo{},
		seenFiles: map[string]bool{},
	}
	for _, pkg := range prog.Pkgs {
		if goprog.IsTestMain(pkg) || pkg.TypesInfo == nil || pkg.Types == nil {
			continue
		}
		x.pkg(pkg)
	}
	x.implements()
	x.tests()
	return x.c
}

func (x *extractor) pkg(pkg *packages.Package) {
	repo := x.prog.Repo
	testVariant := goprog.IsTestVariant(pkg)
	rel := x.prog.RelPkg(pkg.PkgPath)
	pkgKey := repo + ":" + rel
	layer := goprog.Layer(rel)
	repoRef := model.Ref{Label: "Repo", Key: repo}
	pkgRef := model.Ref{Label: "Package", Key: pkgKey}
	pkgDone := false
	importsSeen := map[string]bool{}

	for i, file := range pkg.Syntax {
		if i >= len(pkg.CompiledGoFiles) {
			break
		}
		abs := pkg.CompiledGoFiles[i]
		if !x.prog.Inside(abs) {
			continue // cgo-generated files live in the build cache
		}
		relFile := x.prog.RelFile(abs)
		isTest := strings.HasSuffix(relFile, "_test.go")
		if testVariant && !isTest {
			continue // the plain variant already covered the non-test files
		}
		if x.seenFiles[relFile] {
			continue
		}
		x.seenFiles[relFile] = true

		if !pkgDone {
			props := map[string]any{"name": pkg.Name, "repo": repo, "path": rel, "import_path": pkg.PkgPath, "layer": layer}
			if doc := pkgDoc(pkg); doc != "" {
				props["doc"] = doc
			}
			x.c.Node("Package", pkgKey, props)
			x.c.Edge("CONTAINS", repoRef, pkgRef, x.src.Props(relFile, 0, nil))
			pkgDone = true
		}

		fileKey := repo + ":" + relFile
		kind := "source"
		if isTest {
			kind = "test"
		}
		x.c.Node("File", fileKey, map[string]any{
			"name": filepath.Base(relFile), "path": relFile, "repo": repo, "kind": kind, "lang": "go",
			"lines": x.prog.Fset.File(file.Pos()).LineCount(), "package": pkg.PkgPath, "layer": layer,
		})
		fileRef := model.Ref{Label: "File", Key: fileKey}
		x.c.Edge("CONTAINS", pkgRef, fileRef, x.src.Props(relFile, 1, nil))

		for _, imp := range file.Imports {
			path, err := strconv.Unquote(imp.Path.Value)
			if err != nil || !x.prog.InRepo(path) || importsSeen[path] {
				continue
			}
			importsSeen[path] = true
			x.c.Edge("IMPORTS", pkgRef, model.Ref{Label: "Package", Key: repo + ":" + x.prog.RelPkg(path)}, x.src.Props(relFile, x.prog.Line(imp.Pos()), nil))
		}

		for _, decl := range file.Decls {
			switch d := decl.(type) {
			case *ast.FuncDecl:
				x.funcDecl(pkg, d, relFile, fileRef, layer, isTest)
			case *ast.GenDecl:
				x.genDecl(pkg, d, relFile, fileRef, layer, isTest)
			}
		}
	}
}

func pkgDoc(pkg *packages.Package) string {
	for _, f := range pkg.Syntax {
		if f.Doc != nil {
			return strings.TrimSpace(f.Doc.Text())
		}
	}
	return ""
}

func isTestFuncName(name string) bool {
	for _, prefix := range []string{"Test", "Benchmark", "Fuzz", "Example"} {
		if strings.HasPrefix(name, prefix) {
			rest := name[len(prefix):]
			return rest == "" || !(rest[0] >= 'a' && rest[0] <= 'z')
		}
	}
	return false
}

func isMock(relFile, name string) bool {
	base := filepath.Base(relFile)
	return strings.HasPrefix(base, "mock_") || strings.Contains(relFile, "/mocks/") || strings.HasPrefix(name, "Mock") || strings.HasPrefix(name, "mock")
}

func (x *extractor) register(s *symInfo) {
	x.symbols[s.key] = s
	k := s.relPkg + "\x00" + s.name
	x.byPkgName[k] = append(x.byPkgName[k], s)
}

func (x *extractor) baseProps(pkg *packages.Package, obj types.Object, name, kind, relFile, layer string, start, end int, isTest bool) map[string]any {
	return map[string]any{
		"name": name, "fqn": x.prog.SymbolFQN(obj), "kind": kind, "repo": x.prog.Repo, "file": relFile,
		"line_start": start, "line_end": end, "exported": obj.Exported(), "tokens": model.Tokens(name),
		"layer": layer, "package": pkg.PkgPath, "test_file": isTest, "is_mock": isMock(relFile, name), "lang": "go",
	}
}

func (x *extractor) funcDecl(pkg *packages.Package, d *ast.FuncDecl, relFile string, fileRef model.Ref, layer string, isTest bool) {
	obj, ok := pkg.TypesInfo.Defs[d.Name].(*types.Func)
	if !ok || obj == nil {
		return
	}
	key := x.prog.SymbolKey(obj)
	start, end := x.prog.Line(d.Pos()), x.prog.Line(d.End())
	kind, name := "func", d.Name.Name
	recv := goprog.RecvTypeName(obj)
	if d.Recv != nil && recv != nil {
		kind = "method"
		name = recv.Name() + "." + d.Name.Name
	}
	if isTest && kind == "func" && isTestFuncName(d.Name.Name) {
		kind = "test"
	}
	props := x.baseProps(pkg, obj, name, kind, relFile, layer, start, end, isTest)
	sig := strings.TrimPrefix(types.TypeString(obj.Type(), goprog.Qualifier(pkg.Types)), "func")
	if recv != nil {
		props["signature"] = "(" + recvPrefix(obj) + recv.Name() + ")." + d.Name.Name + sig
	} else {
		props["signature"] = d.Name.Name + sig
	}
	if d.Doc != nil {
		props["doc"] = strings.TrimSpace(d.Doc.Text())
	}
	x.c.Node("Symbol", key, props)
	symRef := model.Ref{Label: "Symbol", Key: key}
	x.c.Edge("CONTAINS", fileRef, symRef, x.src.Props(relFile, start, nil))
	if recv != nil && recv.Pkg() != nil && x.prog.InRepo(recv.Pkg().Path()) {
		x.c.Edge("CONTAINS", model.Ref{Label: "Symbol", Key: x.prog.SymbolKey(recv)}, symRef, x.src.Props(relFile, start, nil))
	}
	info := &symInfo{key: key, name: name, relPkg: x.prog.RelPkg(pkg.PkgPath), kind: kind, file: relFile, line: start, testFile: isTest, obj: obj}
	x.register(info)
	x.body(pkg, d, d.Type, d.Recv, info, relFile)
}

func recvPrefix(fn *types.Func) string {
	if sig, ok := fn.Type().(*types.Signature); ok && sig.Recv() != nil {
		if _, ptr := sig.Recv().Type().(*types.Pointer); ptr {
			return "*"
		}
	}
	return ""
}

// body records CALLS and REFERENCES made inside a declaration. Closures are
// attributed to the enclosing declaration.
func (x *extractor) body(pkg *packages.Package, node ast.Node, sig *ast.FuncType, recv *ast.FieldList, from *symInfo, relFile string) {
	info := pkg.TypesInfo
	callPos := map[token.Pos]bool{}
	litTypes := map[token.Pos]bool{}
	ast.Inspect(node, func(n ast.Node) bool {
		switch n := n.(type) {
		case *ast.CallExpr:
			switch f := ast.Unparen(n.Fun).(type) {
			case *ast.Ident:
				callPos[f.Pos()] = true
			case *ast.SelectorExpr:
				callPos[f.Sel.Pos()] = true
			}
		case *ast.CompositeLit:
			if n.Type != nil {
				ast.Inspect(n.Type, func(t ast.Node) bool {
					if id, ok := t.(*ast.Ident); ok {
						litTypes[id.Pos()] = true
					}
					return true
				})
			}
		}
		return true
	})
	inRange := func(pos token.Pos, n ast.Node) bool {
		return n != nil && pos >= n.Pos() && pos < n.End()
	}
	fromRef := model.Ref{Label: "Symbol", Key: from.key}
	seenRef := map[string]bool{}
	ast.Inspect(node, func(n ast.Node) bool {
		id, ok := n.(*ast.Ident)
		if !ok {
			return true
		}
		obj := info.Uses[id]
		if obj == nil || obj.Pkg() == nil || !x.prog.InRepo(obj.Pkg().Path()) || !declaredAtPackageLevel(obj) {
			return true
		}
		line := x.prog.Line(id.Pos())
		switch o := obj.(type) {
		case *types.Func:
			toKey := x.prog.SymbolKey(o)
			if toKey == from.key {
				return true
			}
			via := "value"
			if callPos[id.Pos()] {
				via = "static"
				if goprog.IsInterfaceMethod(o) {
					via = "interface"
				}
			}
			x.c.Edge("CALLS", fromRef, model.Ref{Label: "Symbol", Key: toKey}, x.src.Props(relFile, line, map[string]any{"via": via}))
			if from.kind == "test" {
				x.testCalls = append(x.testCalls, testCall{from: from, to: toKey, file: relFile, line: line})
			}
		case *types.TypeName:
			if recv != nil && inRange(id.Pos(), recv) {
				return true
			}
			how := "uses"
			switch {
			case sig != nil && sig.Results != nil && inRange(id.Pos(), sig.Results):
				how = "return"
			case sig != nil && sig.Params != nil && inRange(id.Pos(), sig.Params):
				how = "param"
			case litTypes[id.Pos()]:
				how = "instantiates"
			}
			toKey := x.prog.SymbolKey(o)
			if toKey == from.key || seenRef[toKey+"|"+how] {
				return true
			}
			seenRef[toKey+"|"+how] = true
			x.c.Edge("REFERENCES", fromRef, model.Ref{Label: "Symbol", Key: toKey}, x.src.Props(relFile, line, map[string]any{"how": how}))
		}
		return true
	})
}

// declaredAtPackageLevel excludes what has no Symbol node: type parameters,
// types declared inside functions, and methods of anonymous interface types.
func declaredAtPackageLevel(obj types.Object) bool {
	switch o := obj.(type) {
	case *types.Func:
		if sig, ok := o.Type().(*types.Signature); ok && sig.Recv() != nil {
			tn := goprog.RecvTypeName(o)
			return tn != nil && tn.Pkg() != nil && tn.Parent() == tn.Pkg().Scope()
		}
		return o.Pkg() != nil && o.Parent() == o.Pkg().Scope()
	case *types.TypeName:
		return o.Pkg() != nil && o.Parent() == o.Pkg().Scope()
	}
	return true
}

func (x *extractor) genDecl(pkg *packages.Package, d *ast.GenDecl, relFile string, fileRef model.Ref, layer string, isTest bool) {
	for _, spec := range d.Specs {
		switch s := spec.(type) {
		case *ast.TypeSpec:
			x.typeSpec(pkg, d, s, relFile, fileRef, layer, isTest)
		case *ast.ValueSpec:
			x.valueSpec(pkg, d, s, relFile, fileRef, layer, isTest)
		}
	}
}

func (x *extractor) valueSpec(pkg *packages.Package, d *ast.GenDecl, s *ast.ValueSpec, relFile string, fileRef model.Ref, layer string, isTest bool) {
	for _, name := range s.Names {
		if name.Name == "_" {
			continue
		}
		obj := pkg.TypesInfo.Defs[name]
		if obj == nil {
			continue
		}
		kind := "var"
		if _, ok := obj.(*types.Const); ok {
			kind = "const"
		}
		key := x.prog.SymbolKey(obj)
		start, end := x.prog.Line(s.Pos()), x.prog.Line(s.End())
		props := x.baseProps(pkg, obj, name.Name, kind, relFile, layer, start, end, isTest)
		props["type"] = types.TypeString(obj.Type(), goprog.Qualifier(pkg.Types))
		if c, ok := obj.(*types.Const); ok && c.Val() != nil {
			if c.Val().Kind() == constant.String {
				props["value"] = constant.StringVal(c.Val())
			} else {
				props["value"] = c.Val().String()
			}
		}
		if doc := specDoc(d, s.Doc); doc != "" {
			props["doc"] = doc
		}
		x.c.Node("Symbol", key, props)
		x.c.Edge("CONTAINS", fileRef, model.Ref{Label: "Symbol", Key: key}, x.src.Props(relFile, start, nil))
		info := &symInfo{key: key, name: name.Name, relPkg: x.prog.RelPkg(pkg.PkgPath), kind: kind, file: relFile, line: start, testFile: isTest, obj: obj}
		x.register(info)
		for _, v := range s.Values {
			x.body(pkg, v, nil, nil, info, relFile)
		}
	}
}

func specDoc(d *ast.GenDecl, own *ast.CommentGroup) string {
	if own != nil {
		return strings.TrimSpace(own.Text())
	}
	if d.Doc != nil && len(d.Specs) == 1 {
		return strings.TrimSpace(d.Doc.Text())
	}
	return ""
}

func (x *extractor) typeSpec(pkg *packages.Package, d *ast.GenDecl, s *ast.TypeSpec, relFile string, fileRef model.Ref, layer string, isTest bool) {
	obj, ok := pkg.TypesInfo.Defs[s.Name].(*types.TypeName)
	if !ok || obj == nil {
		return
	}
	key := x.prog.SymbolKey(obj)
	start, end := x.prog.Line(s.Pos()), x.prog.Line(s.End())
	under := types.Unalias(obj.Type()).Underlying()
	kind := "type"
	iface, isIface := under.(*types.Interface)
	if isIface {
		kind = "interface"
	}
	props := x.baseProps(pkg, obj, s.Name.Name, kind, relFile, layer, start, end, isTest)
	qual := goprog.Qualifier(pkg.Types)
	underlying := types.TypeString(under, qual)
	if len(underlying) > 300 {
		underlying = underlying[:300] + "…"
	}
	props["underlying"] = underlying
	props["signature"] = underlying
	if doc := specDoc(d, s.Doc); doc != "" {
		props["doc"] = doc
	}
	symRef := model.Ref{Label: "Symbol", Key: key}

	if st, ok := under.(*types.Struct); ok && !obj.IsAlias() {
		var fields, jsonFields []string
		for i := 0; i < st.NumFields(); i++ {
			f := st.Field(i)
			fields = append(fields, f.Name())
			if tag := reflect.StructTag(st.Tag(i)).Get("json"); tag != "" && tag != "-" {
				jsonName := strings.Split(tag, ",")[0]
				if jsonName == "" {
					jsonName = f.Name()
				}
				if !f.Embedded() {
					jsonFields = append(jsonFields, jsonName)
				}
			}
			if tn := goprog.NamedTypeName(f.Type()); tn != nil && tn.Pkg() != nil && x.prog.InRepo(tn.Pkg().Path()) {
				how := "field"
				if f.Embedded() {
					how = "embeds"
				}
				toKey := x.prog.SymbolKey(tn)
				if toKey != key {
					x.c.Edge("REFERENCES", symRef, model.Ref{Label: "Symbol", Key: toKey}, x.src.Props(relFile, x.prog.Line(f.Pos()), map[string]any{"how": how, "field": f.Name()}))
				}
			}
		}
		if len(fields) > 0 {
			props["fields"] = fields
		}
		if len(jsonFields) > 0 {
			props["json_fields"] = jsonFields
		}
	}
	x.c.Node("Symbol", key, props)
	x.c.Edge("CONTAINS", fileRef, symRef, x.src.Props(relFile, start, nil))
	x.register(&symInfo{key: key, name: s.Name.Name, relPkg: x.prog.RelPkg(pkg.PkgPath), kind: kind, file: relFile, line: start, testFile: isTest, obj: obj})

	if isIface {
		for i := 0; i < iface.NumExplicitMethods(); i++ {
			m := iface.ExplicitMethod(i)
			mkey := x.prog.SymbolKey(m)
			mname := s.Name.Name + "." + m.Name()
			mline := x.prog.Line(m.Pos())
			mprops := x.baseProps(pkg, m, mname, "interface_method", relFile, layer, mline, mline, isTest)
			mprops["signature"] = mname + strings.TrimPrefix(types.TypeString(m.Type(), qual), "func")
			if doc := methodDoc(s, m.Name()); doc != "" {
				mprops["doc"] = doc
			}
			x.c.Node("Symbol", mkey, mprops)
			x.c.Edge("CONTAINS", symRef, model.Ref{Label: "Symbol", Key: mkey}, x.src.Props(relFile, mline, nil))
			x.register(&symInfo{key: mkey, name: mname, relPkg: x.prog.RelPkg(pkg.PkgPath), kind: "interface_method", file: relFile, line: mline, testFile: isTest, obj: m})
		}
		for i := 0; i < iface.NumEmbeddeds(); i++ {
			if tn := goprog.NamedTypeName(iface.EmbeddedType(i)); tn != nil && tn.Pkg() != nil && x.prog.InRepo(tn.Pkg().Path()) {
				x.c.Edge("REFERENCES", symRef, model.Ref{Label: "Symbol", Key: x.prog.SymbolKey(tn)}, x.src.Props(relFile, start, map[string]any{"how": "embeds"}))
			}
		}
		if !isTest {
			x.ifaces = append(x.ifaces, obj)
		}
		return
	}
	if !isTest && !obj.IsAlias() {
		if _, named := obj.Type().(*types.Named); named {
			x.named = append(x.named, obj)
		}
	}
}

// methodDoc finds the comment above a method inside an interface literal.
func methodDoc(s *ast.TypeSpec, name string) string {
	it, ok := s.Type.(*ast.InterfaceType)
	if !ok || it.Methods == nil {
		return ""
	}
	for _, f := range it.Methods.List {
		for _, n := range f.Names {
			if n.Name == name && f.Doc != nil {
				return strings.TrimSpace(f.Doc.Text())
			}
		}
	}
	return ""
}

// implements links every concrete type to the repository interfaces it satisfies,
// and each of its methods to the interface method it fulfils.
func (x *extractor) implements() {
	for _, tn := range x.named {
		ptr := types.NewPointer(tn.Type())
		if types.NewMethodSet(ptr).Len() == 0 {
			continue
		}
		tInfo := x.symbols[x.prog.SymbolKey(tn)]
		if tInfo == nil {
			continue
		}
		for _, in := range x.ifaces {
			iface, ok := types.Unalias(in.Type()).Underlying().(*types.Interface)
			if !ok || iface.NumMethods() == 0 || !types.Implements(ptr, iface) {
				continue
			}
			x.c.Edge("IMPLEMENTS", model.Ref{Label: "Symbol", Key: tInfo.key}, model.Ref{Label: "Symbol", Key: x.prog.SymbolKey(in)}, x.src.Props(tInfo.file, tInfo.line, nil))
			for i := 0; i < iface.NumMethods(); i++ {
				im := iface.Method(i)
				if im.Pkg() == nil || !x.prog.InRepo(im.Pkg().Path()) {
					continue
				}
				obj, _, _ := types.LookupFieldOrMethod(ptr, true, im.Pkg(), im.Name())
				fn, ok := obj.(*types.Func)
				if !ok || goprog.IsInterfaceMethod(fn) || fn.Pkg() == nil || !x.prog.InRepo(fn.Pkg().Path()) {
					continue
				}
				x.c.Edge("IMPLEMENTS", model.Ref{Label: "Symbol", Key: x.prog.SymbolKey(fn)}, model.Ref{Label: "Symbol", Key: x.prog.SymbolKey(im)},
					x.src.Props(x.prog.File(fn.Pos()), x.prog.Line(fn.Pos()), nil))
			}
		}
	}
}

// tests links test functions to what they exercise: direct callees, and symbols
// named in the test name (TestOrder_MerchantName -> Order.MerchantName).
func (x *extractor) tests() {
	seen := map[string]bool{}
	for _, tc := range x.testCalls {
		to := x.symbols[tc.to]
		if to == nil || to.testFile || to.kind == "interface_method" || seen[tc.from.key+"|"+to.key] {
			continue
		}
		seen[tc.from.key+"|"+to.key] = true
		x.c.Edge("TESTS", model.Ref{Label: "Symbol", Key: tc.from.key}, model.Ref{Label: "Symbol", Key: to.key}, x.src.Props(tc.file, tc.line, map[string]any{"how": "calls"}))
	}
	for _, s := range x.symbols {
		if s.kind != "test" {
			continue
		}
		name := s.name
		for _, prefix := range []string{"Test", "Benchmark", "Fuzz", "Example"} {
			if strings.HasPrefix(name, prefix) {
				name = name[len(prefix):]
				break
			}
		}
		parts := strings.SplitN(name, "_", 3)
		if parts[0] == "" {
			continue
		}
		pkgUnderTest := strings.TrimSuffix(s.relPkg, "_test")
		candidates := append([]*symInfo{}, x.byPkgName[pkgUnderTest+"\x00"+parts[0]]...)
		if len(parts) > 1 {
			candidates = append(candidates, x.byPkgName[pkgUnderTest+"\x00"+parts[0]+"."+parts[1]]...)
		}
		for _, cand := range candidates {
			if cand.testFile || cand.key == s.key {
				continue
			}
			x.c.Edge("TESTS", model.Ref{Label: "Symbol", Key: s.key}, model.Ref{Label: "Symbol", Key: cand.key}, x.src.Props(s.file, s.line, map[string]any{"how": "names"}))
		}
	}
}
