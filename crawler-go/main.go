// crawler-go analyses one Go repository and prints the claims JSON that
// scripts/indexer.py loads into FalkorDB. It covers L1 (packages, files,
// symbols, CALLS, IMPLEMENTS, REFERENCES, TESTS) and the HTTP routes of L2.
package main

import (
	"flag"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"graphify/crawler-go/internal/gocode"
	"graphify/crawler-go/internal/goprog"
	"graphify/crawler-go/internal/model"
	"graphify/crawler-go/internal/routes"
)

func main() {
	repo := flag.String("repo", "", "path to the repository (required)")
	name := flag.String("name", "", "repository name used in keys (default: directory name)")
	commit := flag.String("commit", "", "commit sha for source_ref (default: git rev-parse --short HEAD)")
	out := flag.String("out", "-", "output file for claims JSON, - for stdout")
	withTests := flag.Bool("tests", true, "include _test.go files")
	withRoutes := flag.Bool("routes", true, "extract HTTP route registrations")
	verbose := flag.Bool("v", false, "progress on stderr")
	flag.Parse()

	if *repo == "" {
		fmt.Fprintln(os.Stderr, "usage: crawler-go -repo <path> [-name <repo>] [-commit <sha>] [-out claims.json]")
		os.Exit(2)
	}
	root, err := filepath.Abs(*repo)
	if err != nil {
		fail(err)
	}
	if *name == "" {
		*name = filepath.Base(root)
	}
	if *commit == "" {
		*commit = gitShort(root)
	}

	started := time.Now()
	prog, err := goprog.Load(*name, root, *withTests)
	if err != nil {
		fail(err)
	}
	if *verbose {
		fmt.Fprintf(os.Stderr, "loaded %d packages (%d type errors) in %s\n", len(prog.Pkgs), prog.Errors, time.Since(started).Round(time.Millisecond))
	}

	all := model.New()
	code := gocode.Extract(prog, model.Source{Repo: *name, Commit: *commit, ExtractedBy: "crawler-go/code"})
	merge(all, code)
	if *withRoutes {
		merge(all, routes.Extract(prog, model.Source{Repo: *name, Commit: *commit, ExtractedBy: "crawler-go/routes"}))
	}
	if *verbose {
		fmt.Fprint(os.Stderr, all.Stats())
		fmt.Fprintf(os.Stderr, "done in %s\n", time.Since(started).Round(time.Millisecond))
	}

	w := os.Stdout
	if *out != "-" {
		f, err := os.Create(*out)
		if err != nil {
			fail(err)
		}
		defer f.Close()
		w = f
	}
	if err := all.Write(w); err != nil {
		fail(err)
	}
}

func merge(dst, src *model.Claims) {
	for _, n := range src.Nodes {
		dst.Node(n.Label, n.Key, n.Props)
	}
	for _, e := range src.Edges {
		dst.Edge(e.Type, e.From, e.To, e.Props)
	}
}

func gitShort(root string) string {
	out, err := exec.Command("git", "-C", root, "rev-parse", "--short", "HEAD").Output()
	if err != nil {
		return "nogit"
	}
	return strings.TrimSpace(string(out))
}

func fail(err error) {
	fmt.Fprintln(os.Stderr, "crawler-go:", err)
	os.Exit(1)
}
