// Package goprog loads a Go repository once so that the extractors share the
// type-checked packages, and defines how Go objects are named in the graph.
package goprog

import (
	"fmt"
	"go/token"
	"go/types"
	"os"
	"path/filepath"
	"regexp"
	"strings"

	"golang.org/x/tools/go/packages"
)

// Program is a loaded repository.
type Program struct {
	Repo   string
	Root   string
	Module string
	Fset   *token.FileSet
	Pkgs   []*packages.Package
	Errors int
}

// Load type-checks every package under root. Dependencies come from export
// data, so only the repository's own files are parsed.
func Load(repo, root string, withTests bool) (*Program, error) {
	fset := token.NewFileSet()
	cfg := &packages.Config{
		Dir:   root,
		Fset:  fset,
		Tests: withTests,
		Mode: packages.NeedName | packages.NeedFiles | packages.NeedCompiledGoFiles |
			packages.NeedImports | packages.NeedTypes | packages.NeedSyntax |
			packages.NeedTypesInfo | packages.NeedModule,
	}
	pkgs, err := packages.Load(cfg, "./...")
	if err != nil {
		return nil, fmt.Errorf("load packages: %w", err)
	}
	p := &Program{Repo: repo, Root: root, Fset: fset, Pkgs: pkgs}
	for _, pkg := range pkgs {
		if pkg.Module != nil && pkg.Module.Main {
			p.Module = pkg.Module.Path
			break
		}
	}
	if p.Module == "" {
		p.Module = modulePath(root)
	}
	for _, pkg := range pkgs {
		p.Errors += len(pkg.Errors)
	}
	return p, nil
}

var moduleRe = regexp.MustCompile(`(?m)^module\s+(\S+)`)

func modulePath(root string) string {
	b, err := os.ReadFile(filepath.Join(root, "go.mod"))
	if err != nil {
		return ""
	}
	if m := moduleRe.FindSubmatch(b); m != nil {
		return string(m[1])
	}
	return ""
}

// InRepo reports whether an import path belongs to the repository's module.
func (p *Program) InRepo(path string) bool {
	return path == p.Module || strings.HasPrefix(path, p.Module+"/")
}

// RelPkg strips the module path: github.com/x/repo/internal/a -> internal/a.
func (p *Program) RelPkg(path string) string {
	if path == p.Module {
		return "."
	}
	return strings.TrimPrefix(path, p.Module+"/")
}

// RelFile makes a file path relative to the repository root, with forward slashes.
func (p *Program) RelFile(abs string) string {
	rel, err := filepath.Rel(p.Root, abs)
	if err != nil {
		return filepath.ToSlash(abs)
	}
	return filepath.ToSlash(rel)
}

// Inside reports whether an absolute path lies under the repository root.
func (p *Program) Inside(abs string) bool {
	return strings.HasPrefix(abs, p.Root+string(filepath.Separator))
}

// Line returns the 1-based line of a position.
func (p *Program) Line(pos token.Pos) int {
	if !pos.IsValid() {
		return 0
	}
	return p.Fset.Position(pos).Line
}

// File returns the repository-relative file of a position.
func (p *Program) File(pos token.Pos) string {
	if !pos.IsValid() {
		return ""
	}
	return p.RelFile(p.Fset.Position(pos).Filename)
}

// IsTestVariant reports whether pkg is the variant go/packages builds for tests:
// "pkg [pkg.test]" or "pkg_test [pkg.test]".
func IsTestVariant(pkg *packages.Package) bool { return strings.Contains(pkg.ID, " [") }

// IsTestMain reports whether pkg is the synthesized test main package.
func IsTestMain(pkg *packages.Package) bool { return strings.HasSuffix(pkg.ID, ".test") }

// Layer maps a repository-relative package path to its architectural layer:
// internal/usecase/receipt -> usecase, pkg/uuid -> pkg, cmd/api -> cmd.
func Layer(relPkg string) string {
	segs := strings.Split(relPkg, "/")
	switch {
	case segs[0] == "internal" && len(segs) > 1:
		return segs[1]
	case segs[0] == "e2e_tests" || segs[0] == "test" || segs[0] == "tests" || segs[0] == "e2e":
		return "tests"
	}
	return segs[0]
}

// SymbolFQN names an object the way SSA does, with the module path stripped:
// internal/a.Func, (internal/a.T).M, (*internal/a.T).M.
func (p *Program) SymbolFQN(obj types.Object) string {
	pkg := obj.Pkg()
	if pkg == nil {
		return obj.Name()
	}
	rel := p.RelPkg(pkg.Path())
	if fn, ok := obj.(*types.Func); ok {
		if sig, ok := fn.Type().(*types.Signature); ok && sig.Recv() != nil {
			rt := sig.Recv().Type()
			ptr := false
			if pt, ok := rt.(*types.Pointer); ok {
				rt = pt.Elem()
				ptr = true
			}
			if ptr {
				return fmt.Sprintf("(*%s.%s).%s", rel, TypeName(rt), fn.Name())
			}
			return fmt.Sprintf("(%s.%s).%s", rel, TypeName(rt), fn.Name())
		}
	}
	return rel + "." + obj.Name()
}

// SymbolKey is the graph key of an object: <repo>:<fqn>.
func (p *Program) SymbolKey(obj types.Object) string {
	return p.Repo + ":" + p.SymbolFQN(obj)
}

// TypeName returns the declared name of a named type, or a rendering of an unnamed one.
func TypeName(t types.Type) string {
	switch t := t.(type) {
	case *types.Named:
		return t.Obj().Name()
	case *types.Alias:
		return t.Obj().Name()
	case *types.Interface:
		return "interface"
	}
	return types.TypeString(t, nil)
}

// NamedTypeName returns the TypeName behind t after stripping pointers, slices,
// arrays, maps and channels; nil for unnamed or basic types.
func NamedTypeName(t types.Type) *types.TypeName {
	for {
		switch tt := t.(type) {
		case *types.Pointer:
			t = tt.Elem()
		case *types.Slice:
			t = tt.Elem()
		case *types.Array:
			t = tt.Elem()
		case *types.Map:
			t = tt.Elem()
		case *types.Chan:
			t = tt.Elem()
		case *types.Named:
			return tt.Obj()
		case *types.Alias:
			return tt.Obj()
		default:
			return nil
		}
	}
}

// RecvTypeName returns the receiver's TypeName for a method, nil for functions.
func RecvTypeName(fn *types.Func) *types.TypeName {
	sig, ok := fn.Type().(*types.Signature)
	if !ok || sig.Recv() == nil {
		return nil
	}
	return NamedTypeName(sig.Recv().Type())
}

// IsInterfaceMethod reports whether fn is declared on an interface type.
func IsInterfaceMethod(fn *types.Func) bool {
	sig, ok := fn.Type().(*types.Signature)
	if !ok || sig.Recv() == nil {
		return false
	}
	_, isIface := sig.Recv().Type().Underlying().(*types.Interface)
	return isIface
}

// Qualifier renders types from pkg unqualified and others by package name.
func Qualifier(pkg *types.Package) types.Qualifier {
	return func(p *types.Package) string {
		if p == pkg {
			return ""
		}
		return p.Name()
	}
}
