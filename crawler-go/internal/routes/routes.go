// Package routes finds HTTP route registrations (chi, net/http.ServeMux) and
// links each endpoint to the handler symbol that serves it.
package routes

import (
	"go/ast"
	"go/types"
	"strconv"
	"strings"

	"golang.org/x/tools/go/packages"

	"graphify/crawler-go/internal/goprog"
	"graphify/crawler-go/internal/model"
)

var verbs = map[string]string{
	"Get": "GET", "Post": "POST", "Put": "PUT", "Patch": "PATCH", "Delete": "DELETE",
	"Head": "HEAD", "Options": "OPTIONS", "Connect": "CONNECT", "Trace": "TRACE",
	"Handle": "ANY", "HandleFunc": "ANY",
}

type scope struct {
	prefix string
	mws    []string
	depth  int
}

type extractor struct {
	prog *goprog.Program
	src  model.Source
	c    *model.Claims
}

// Extract builds HttpEndpoint nodes and HANDLES edges for every route registration.
func Extract(prog *goprog.Program, src model.Source) *model.Claims {
	x := &extractor{prog: prog, src: src, c: model.New()}
	for _, pkg := range prog.Pkgs {
		if goprog.IsTestVariant(pkg) || goprog.IsTestMain(pkg) || pkg.TypesInfo == nil {
			continue
		}
		for i, file := range pkg.Syntax {
			if i >= len(pkg.CompiledGoFiles) || !prog.Inside(pkg.CompiledGoFiles[i]) {
				continue
			}
			relFile := prog.RelFile(pkg.CompiledGoFiles[i])
			if strings.HasSuffix(relFile, "_test.go") {
				continue
			}
			for _, decl := range file.Decls {
				if fd, ok := decl.(*ast.FuncDecl); ok && fd.Body != nil {
					x.walk(pkg, fd.Body, relFile, &scope{})
				}
			}
		}
	}
	return x.c
}

func (x *extractor) routerKind(pkg *packages.Package, expr ast.Expr) string {
	t := pkg.TypesInfo.TypeOf(expr)
	if t == nil {
		return ""
	}
	s := t.String()
	switch {
	case strings.Contains(s, "github.com/go-chi/chi"):
		return "chi"
	case strings.Contains(s, "net/http.ServeMux"):
		return "servemux"
	}
	return ""
}

// walk registers the routes of one scope. chi's Use applies to every route of
// the router it is called on, so middlewares collected here apply to the routes
// that follow in the same closure.
func (x *extractor) walk(pkg *packages.Package, node ast.Node, relFile string, sc *scope) {
	ast.Inspect(node, func(n ast.Node) bool {
		call, ok := n.(*ast.CallExpr)
		if !ok {
			return true
		}
		sel, ok := call.Fun.(*ast.SelectorExpr)
		if !ok {
			return true
		}
		kind := x.routerKind(pkg, sel.X)
		if kind == "" {
			return true
		}
		name := sel.Sel.Name
		switch {
		case kind == "chi" && name == "Route" && len(call.Args) == 2:
			if p, ok := strLit(call.Args[0]); ok {
				if fl, ok := call.Args[1].(*ast.FuncLit); ok {
					x.walk(pkg, fl.Body, relFile, &scope{prefix: sc.prefix + p, mws: clone(sc.mws), depth: sc.depth + 1})
					return false
				}
			}
		case kind == "chi" && name == "Group" && len(call.Args) == 1:
			if fl, ok := call.Args[0].(*ast.FuncLit); ok {
				x.walk(pkg, fl.Body, relFile, &scope{prefix: sc.prefix, mws: clone(sc.mws), depth: sc.depth + 1})
				return false
			}
		case kind == "chi" && name == "Use":
			if sc.depth > 0 {
				sc.mws = append(sc.mws, exprStrings(call.Args)...)
			}
			return false
		case kind == "chi" && name == "Method" && len(call.Args) == 3:
			method, ok1 := strLit(call.Args[0])
			pattern, ok2 := strLit(call.Args[1])
			if ok1 && ok2 {
				x.emit(pkg, strings.ToUpper(method), sc.prefix+pattern, call.Args[2], append(clone(sc.mws), inlineMiddlewares(sel.X)...), relFile, x.prog.Line(call.Pos()))
			}
			return false
		default:
			method, isVerb := verbs[name]
			if !isVerb || len(call.Args) != 2 {
				return true
			}
			pattern, ok := strLit(call.Args[0])
			if !ok {
				return false
			}
			if _, second := strLit(call.Args[1]); second {
				return false // not a route registration
			}
			if kind == "servemux" {
				// Go 1.22 patterns: "GET /path" or "/path".
				if i := strings.IndexByte(pattern, ' '); i > 0 && strings.ToUpper(pattern[:i]) == pattern[:i] {
					method, pattern = pattern[:i], strings.TrimSpace(pattern[i+1:])
				}
				if i := strings.IndexByte(pattern, '/'); i > 0 { // host-qualified pattern
					pattern = pattern[i:]
				}
			}
			x.emit(pkg, method, sc.prefix+pattern, call.Args[1], append(clone(sc.mws), inlineMiddlewares(sel.X)...), relFile, x.prog.Line(call.Pos()))
			return false
		}
		return true
	})
}

func (x *extractor) emit(pkg *packages.Package, method, path string, handler ast.Expr, mws []string, relFile string, line int) {
	key := model.EndpointKey(method, path)
	props := map[string]any{
		"name": key, "method": method, "path_code": path, "path_pattern": model.NormalizePath(path),
		"in_code": true, "owner_repo": x.prog.Repo, "repo": x.prog.Repo, "source_ref": x.src.Ref(relFile, line),
	}
	if len(mws) > 0 {
		props["middlewares"] = mws
	}
	if rl := rateLimit(mws); rl != "" {
		props["rate_limit"] = rl
	}
	x.c.Node("HttpEndpoint", key, props)
	epRef := model.Ref{Label: "HttpEndpoint", Key: key}
	edgeProps := x.src.Props(relFile, line, map[string]any{"middlewares": mws})
	if hk := x.handlerKey(pkg, handler); hk != "" {
		x.c.Edge("HANDLES", model.Ref{Label: "Symbol", Key: hk}, epRef, edgeProps)
		return
	}
	text := types.ExprString(handler)
	if _, ok := handler.(*ast.FuncLit); ok {
		text = "func literal"
	}
	ukey := x.prog.Repo + ":handler:" + relFile + ":" + strconv.Itoa(line)
	x.c.Node("Unresolved", ukey, map[string]any{
		"name": text, "kind": "handler", "repo": x.prog.Repo, "source_ref": x.src.Ref(relFile, line),
		"reason": "handler is not a declared function in this repository",
	})
	x.c.Edge("HANDLES", model.Ref{Label: "Unresolved", Key: ukey}, epRef, edgeProps)
}

func (x *extractor) handlerKey(pkg *packages.Package, e ast.Expr) string {
	var id *ast.Ident
	switch v := ast.Unparen(e).(type) {
	case *ast.Ident:
		id = v
	case *ast.SelectorExpr:
		id = v.Sel
	case *ast.CallExpr:
		if len(v.Args) == 1 { // http.HandlerFunc(h) and similar conversions
			return x.handlerKey(pkg, v.Args[0])
		}
		return ""
	default:
		return ""
	}
	if fn, ok := pkg.TypesInfo.Uses[id].(*types.Func); ok && fn.Pkg() != nil && x.prog.InRepo(fn.Pkg().Path()) {
		return x.prog.SymbolKey(fn)
	}
	return ""
}

// inlineMiddlewares collects the arguments of a r.With(a).With(b) chain.
func inlineMiddlewares(expr ast.Expr) []string {
	var mws []string
	for {
		call, ok := ast.Unparen(expr).(*ast.CallExpr)
		if !ok {
			return mws
		}
		sel, ok := call.Fun.(*ast.SelectorExpr)
		if !ok || sel.Sel.Name != "With" {
			return mws
		}
		mws = append(exprStrings(call.Args), mws...)
		expr = sel.X
	}
}

func rateLimit(mws []string) string {
	for _, mw := range mws {
		lower := strings.ToLower(mw)
		if i := strings.Index(lower, "ratelimit"); i >= 0 {
			rest := mw[i:]
			if j := strings.IndexByte(rest, '.'); j >= 0 {
				rest = rest[j+1:]
				if k := strings.IndexByte(rest, '.'); k >= 0 {
					rest = rest[:k]
				}
				return model.Snake(rest)
			}
		}
	}
	return ""
}

func strLit(e ast.Expr) (string, bool) {
	lit, ok := ast.Unparen(e).(*ast.BasicLit)
	if !ok || lit.Kind.String() != "STRING" {
		return "", false
	}
	s, err := strconv.Unquote(lit.Value)
	if err != nil {
		return "", false
	}
	return s, true
}

func exprStrings(args []ast.Expr) []string {
	out := make([]string, 0, len(args))
	for _, a := range args {
		out = append(out, types.ExprString(a))
	}
	return out
}

func clone(s []string) []string {
	return append([]string(nil), s...)
}
