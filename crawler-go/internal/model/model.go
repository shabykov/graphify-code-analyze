// Package model is the exchange format between the crawler and the Python
// loader: nodes keyed by (label, key) and edges that each carry one piece of
// evidence. The JSON shape matches scripts/graphify/model.py.
package model

import (
	"crypto/sha1"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"regexp"
	"sort"
	"strings"
	"unicode"
)

// Ref points at a node by label and key.
type Ref struct {
	Label string `json:"label"`
	Key   string `json:"key"`
}

// Node is one graph node; Props always includes "key".
type Node struct {
	Label string         `json:"label"`
	Key   string         `json:"key"`
	Props map[string]any `json:"props"`
}

// Edge is one claim: a typed relation plus the evidence it came from.
type Edge struct {
	Type    string         `json:"type"`
	From    Ref            `json:"from"`
	To      Ref            `json:"to"`
	ClaimID string         `json:"claim_id"`
	Props   map[string]any `json:"props"`
}

// Claims accumulates nodes and edges, merging repeated nodes and dropping repeated claims.
type Claims struct {
	Nodes []*Node `json:"nodes"`
	Edges []*Edge `json:"edges"`

	nodeIdx map[string]*Node
	edgeIdx map[string]*Edge
}

// New returns an empty Claims set.
func New() *Claims {
	return &Claims{nodeIdx: map[string]*Node{}, edgeIdx: map[string]*Edge{}}
}

func nodeID(label, key string) string { return label + "\x00" + key }

// Node adds a node or merges props into the existing one; later non-nil values win.
func (c *Claims) Node(label, key string, props map[string]any) *Node {
	id := nodeID(label, key)
	n, ok := c.nodeIdx[id]
	if !ok {
		n = &Node{Label: label, Key: key, Props: map[string]any{"key": key}}
		c.nodeIdx[id] = n
		c.Nodes = append(c.Nodes, n)
	}
	for k, v := range props {
		if v != nil && v != "" {
			n.Props[k] = v
		}
	}
	return n
}

// Has reports whether a node exists.
func (c *Claims) Has(label, key string) bool {
	_, ok := c.nodeIdx[nodeID(label, key)]
	return ok
}

// Edge adds one claim. The id hashes the relation and its source_ref, so the same
// evidence seen twice stays one edge while new evidence becomes a second edge.
func (c *Claims) Edge(typ string, from, to Ref, props map[string]any) *Edge {
	sourceRef, _ := props["source_ref"].(string)
	sum := sha1.Sum([]byte(strings.Join([]string{typ, from.Label, from.Key, to.Label, to.Key, sourceRef}, "|")))
	id := hex.EncodeToString(sum[:])[:20]
	if e, ok := c.edgeIdx[id]; ok {
		return e
	}
	e := &Edge{Type: typ, From: from, To: to, ClaimID: id, Props: map[string]any{"claim_id": id}}
	for k, v := range props {
		if v != nil && v != "" {
			e.Props[k] = v
		}
	}
	c.edgeIdx[id] = e
	c.Edges = append(c.Edges, e)
	return e
}

// Write encodes the claims as JSON.
func (c *Claims) Write(w io.Writer) error {
	enc := json.NewEncoder(w)
	return enc.Encode(c)
}

// Stats renders node counts per label and edge counts per type.
func (c *Claims) Stats() string {
	nodes := map[string]int{}
	edges := map[string]int{}
	for _, n := range c.Nodes {
		nodes[n.Label]++
	}
	for _, e := range c.Edges {
		edges[e.Type]++
	}
	var b strings.Builder
	fmt.Fprintf(&b, "nodes: %d\n", len(c.Nodes))
	for _, k := range sortedKeys(nodes) {
		fmt.Fprintf(&b, "  %-16s %6d\n", k, nodes[k])
	}
	fmt.Fprintf(&b, "edges: %d\n", len(c.Edges))
	for _, k := range sortedKeys(edges) {
		fmt.Fprintf(&b, "  %-16s %6d\n", k, edges[k])
	}
	return b.String()
}

func sortedKeys(m map[string]int) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}

// Source identifies the scan that produced a set of claims.
type Source struct {
	Repo        string
	Commit      string
	ExtractedBy string
}

// Ref renders <repo>@<sha>:<path>:L<line>; line 0 omits the line.
func (s Source) Ref(path string, line int) string {
	if line > 0 {
		return fmt.Sprintf("%s@%s:%s:L%d", s.Repo, s.Commit, path, line)
	}
	return fmt.Sprintf("%s@%s:%s", s.Repo, s.Commit, path)
}

// Props builds the provenance every claim carries, plus extra properties.
func (s Source) Props(path string, line int, extra map[string]any) map[string]any {
	p := map[string]any{
		"repo":         s.Repo,
		"commit":       s.Commit,
		"source_ref":   s.Ref(path, line),
		"extracted_by": s.ExtractedBy,
		"confidence":   1.0,
	}
	if line > 0 {
		p["line"] = line
	}
	for k, v := range extra {
		if v != nil && v != "" {
			p[k] = v
		}
	}
	return p
}

var pathParamRe = regexp.MustCompile(`\$\{[^}]*\}|\{[^}]*\}|:[A-Za-z_][A-Za-z0-9_]*|\*+`)

// NormalizePath replaces every path parameter with {} so chi ({id}), OpenAPI
// ({receipt_id}), a TS template literal (${id}) and a glob (*) give one key.
func NormalizePath(p string) string {
	p = pathParamRe.ReplaceAllString(p, "{}")
	for strings.Contains(p, "//") {
		p = strings.ReplaceAll(p, "//", "/")
	}
	if len(p) > 1 {
		p = strings.TrimRight(p, "/")
	}
	if p == "" {
		return "/"
	}
	return p
}

// EndpointKey is the canonical HttpEndpoint key: "GET /api/v1/receipts/{}".
func EndpointKey(method, path string) string {
	return strings.ToUpper(method) + " " + NormalizePath(path)
}

// Tokens splits an identifier into lowercase words for full-text search:
// GetByID -> "get by id", store_name -> "store name", OTPRequest -> "otp request".
func Tokens(s string) string {
	var words []string
	var cur []rune
	flush := func() {
		if len(cur) > 0 {
			words = append(words, strings.ToLower(string(cur)))
			cur = cur[:0]
		}
	}
	rs := []rune(s)
	for i, r := range rs {
		if !unicode.IsLetter(r) && !unicode.IsDigit(r) {
			flush()
			continue
		}
		if len(cur) > 0 && unicode.IsUpper(r) {
			prev := rs[i-1]
			var next rune
			if i+1 < len(rs) {
				next = rs[i+1]
			}
			if unicode.IsLower(prev) || unicode.IsDigit(prev) || (unicode.IsUpper(prev) && unicode.IsLower(next)) {
				flush()
			}
		}
		cur = append(cur, r)
	}
	flush()
	return strings.Join(words, " ")
}

// Snake renders an identifier in snake_case: OTPRequest -> otp_request.
func Snake(s string) string {
	return strings.ReplaceAll(Tokens(s), " ", "_")
}
