#!/usr/bin/env node
// crawler-ts analyses one TypeScript / SvelteKit repository and prints the claims
// JSON that scripts/indexer.py loads into FalkorDB: L1 (packages, files, symbols,
// CALLS, REFERENCES, IMPORTS, TESTS) and the client side of L2 (CALLS_ENDPOINT,
// UiRoute, HANDLES, MOCKS, VISITS, MIRRORS, USES_FIELD).
//
// Svelte components are analysed through their <script> blocks: the markup is
// blanked out so TypeScript positions equal positions in the .svelte file.
import fs from 'node:fs';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { createRequire } from 'node:module';
import { Claims, Source, endpointKey, normalizePath, tokens } from './lib/model.mjs';

const require = createRequire(import.meta.url);
const ts = require('typescript');

// ---------------------------------------------------------------- arguments

function parseArgs(argv) {
  const a = { repo: '', name: '', commit: '', out: '-', apiPrefix: '', schemas: '', verbose: false, srcDirs: ['src', 'tests', 'test', 'e2e'] };
  for (let i = 0; i < argv.length; i++) {
    const k = argv[i];
    const v = argv[i + 1];
    if (k === '--repo') a.repo = v, i++;
    else if (k === '--name') a.name = v, i++;
    else if (k === '--commit') a.commit = v, i++;
    else if (k === '--out') a.out = v, i++;
    else if (k === '--api-prefix') a.apiPrefix = v, i++;
    else if (k === '--schemas') a.schemas = v, i++;
    else if (k === '--src') a.srcDirs = v.split(','), i++;
    else if (k === '-v' || k === '--verbose') a.verbose = true;
  }
  if (!a.repo) {
    console.error('usage: crawler-ts --repo <path> [--name <repo>] [--commit <sha>] [--out claims.json] [--api-prefix /api/v1] [--schemas schemas.json] [-v]');
    process.exit(2);
  }
  return a;
}

function gitShort(root) {
  try {
    return execFileSync('git', ['-C', root, 'rev-parse', '--short', 'HEAD'], { encoding: 'utf8' }).trim();
  } catch {
    return 'nogit';
  }
}

// The API base URL lives in the environment (VITE_API_URL=…/api/v1); its path is the
// prefix every relative client path gets.
function apiPrefixFromEnv(root) {
  for (const f of ['.env', '.env.example', '.env.local', '.env.development', '.env.prod', '.env.production']) {
    try {
      const txt = fs.readFileSync(path.join(root, f), 'utf8');
      const m = txt.match(/^(?:VITE_|PUBLIC_|NEXT_PUBLIC_)?(?:API_URL|API_BASE|API_BASE_URL|BACKEND_URL)\s*=\s*"?([^"\n]+)"?/m);
      if (m) {
        const url = m[1].trim();
        if (url.startsWith('/')) return url.replace(/\/+$/, '');
        try { return new URL(url).pathname.replace(/\/+$/, ''); } catch { /* not a URL */ }
      }
    } catch { /* no such file */ }
  }
  return '';
}

// ---------------------------------------------------------------- file discovery

const SKIP_DIRS = new Set(['node_modules', '.git', '.svelte-kit', '.svelte-kit-vitest', 'build', 'dist', 'coverage', 'playwright-report', 'test-results', '.playwright-mcp', 'static', 'public']);

function walk(dir, out) {
  let entries;
  try { entries = fs.readdirSync(dir, { withFileTypes: true }); } catch { return out; }
  for (const e of entries) {
    if (e.name.startsWith('.') && e.name !== '.') continue;
    const full = path.join(dir, e.name);
    if (e.isDirectory()) {
      if (!SKIP_DIRS.has(e.name)) walk(full, out);
    } else if (/\.(ts|tsx|js|jsx|mjs|cjs|svelte)$/.test(e.name) && !e.name.endsWith('.d.ts')) {
      out.push(full);
    }
  }
  return out;
}

// Replace everything outside <script> blocks with spaces, keeping newlines, so the
// TypeScript positions of the virtual file equal positions in the .svelte file.
function svelteScriptOnly(text) {
  const out = text.replace(/[^\n]/g, ' ').split('');
  const re = /<script\b[^>]*>([\s\S]*?)<\/script>/g;
  let m;
  while ((m = re.exec(text)) !== null) {
    const start = m.index + m[0].indexOf(m[1]);
    for (let i = 0; i < m[1].length; i++) out[start + i] = m[1][i];
  }
  return out.join('');
}

function svelteMarkupOnly(text) {
  return text.replace(/<script\b[^>]*>[\s\S]*?<\/script>/g, (s) => s.replace(/[^\n]/g, ' '));
}

// ---------------------------------------------------------------- program

function createProgram(root, files, virtual) {
  const configPath = ts.findConfigFile(root, ts.sys.fileExists, 'tsconfig.json');
  let options = { allowJs: true, checkJs: false, noEmit: true, skipLibCheck: true, moduleResolution: ts.ModuleResolutionKind.Bundler, module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022, strict: false };
  if (configPath) {
    const cfg = ts.readConfigFile(configPath, ts.sys.readFile);
    if (cfg.config) {
      const parsed = ts.parseJsonConfigFileContent(cfg.config, ts.sys, path.dirname(configPath));
      options = { ...parsed.options, noEmit: true, skipLibCheck: true, allowJs: true, checkJs: false };
    }
  }
  const host = ts.createCompilerHost(options, true);
  const origRead = host.readFile;
  const origExists = host.fileExists;
  host.readFile = (f) => (virtual.has(f) ? virtual.get(f) : origRead(f));
  host.fileExists = (f) => virtual.has(f) || origExists(f);
  const rootNames = [...files.filter((f) => !f.endsWith('.svelte')), ...virtual.keys()];
  return { program: ts.createProgram({ rootNames, options, host }), options, host };
}

// ---------------------------------------------------------------- extraction

class Extractor {
  constructor(a) {
    this.root = path.resolve(a.repo);
    this.repo = a.name || path.basename(this.root);
    this.src = new Source(this.repo, a.commit || gitShort(this.root), 'crawler-ts');
    this.apiPrefix = a.apiPrefix || apiPrefixFromEnv(this.root);
    this.verbose = a.verbose;
    this.c = new Claims();
    this.schemas = null; // { owner, schemas: { Name: [fields] } }
    if (a.schemas) {
      try { this.schemas = JSON.parse(fs.readFileSync(a.schemas, 'utf8')); } catch (e) { this.log(`cannot read schemas: ${e.message}`); }
    }
    this.symbols = new Map(); // key -> info
    this.byPos = new Map(); // `${rel}:${nameStart}` -> key
    this.routes = new Map(); // normalized route pattern -> UiRoute key
    this.testCalls = [];
    this.mirrored = new Map(); // type name -> schema name
    this.filesScanned = 0;
  }

  log(...a) { if (this.verbose) console.error(...a); }
  rel(abs) { return path.relative(this.root, abs).split(path.sep).join('/'); }
  fileKey(rel) { return `${this.repo}:${rel}`; }

  layerOf(rel) {
    const segs = rel.split('/');
    if (segs[0] === 'src') {
      if (segs[1] === 'lib' && segs.length > 3) return segs[2];
      return segs[1] || 'src';
    }
    if (/^(tests?|e2e)$/.test(segs[0])) return 'tests';
    return segs[0];
  }

  run() {
    const absFiles = walk(this.root, []).filter((f) => {
      const rel = this.rel(f);
      return rel.startsWith('src/') || /^(tests?|e2e)\//.test(rel) || !rel.includes('/');
    });
    const virtual = new Map();
    const svelteOf = new Map(); // virtual name -> real abs path
    for (const f of absFiles) {
      if (f.endsWith('.svelte')) {
        const v = `${f}.ts`;
        virtual.set(v, svelteScriptOnly(fs.readFileSync(f, 'utf8')));
        svelteOf.set(v, f);
      }
    }
    const { program, options, host } = createProgram(this.root, absFiles, virtual);
    this.program = program;
    this.options = options;
    this.host = host;
    this.checker = program.getTypeChecker();
    this.svelteOf = svelteOf;

    const sources = program.getSourceFiles().filter((sf) => {
      const real = svelteOf.get(sf.fileName) || sf.fileName;
      return real.startsWith(this.root + path.sep) && !real.includes('/node_modules/') && !sf.isDeclarationFile;
    });

    // Pass 1: structure and symbol index (needed before any cross-file link).
    const units = sources.map((sf) => this.declare(sf));
    // Pass 2: links.
    for (const u of units) this.link(u);
    this.tests();
    this.log(`  ${this.filesScanned} files, ${this.symbols.size} symbols, api prefix '${this.apiPrefix}'`);
    return this.c;
  }

  // ---- pass 1 --------------------------------------------------------------

  declare(sf) {
    const real = this.svelteOf.get(sf.fileName) || sf.fileName;
    const rel = this.rel(real);
    const isSvelte = real.endsWith('.svelte');
    const isTest = /\.(test|spec)\.[cm]?[jt]sx?$/.test(rel) || /^(tests?|e2e)\//.test(rel);
    const layer = this.layerOf(rel);
    const dir = path.posix.dirname(rel);
    const pkgKey = `${this.repo}:${dir}`;
    const fileKey = this.fileKey(rel);
    this.filesScanned++;

    this.c.node('Package', pkgKey, { name: path.posix.basename(dir), repo: this.repo, path: dir, layer });
    this.c.edge('CONTAINS', ['Repo', this.repo], ['Package', pkgKey], this.src.props(rel));
    const text = sf.getFullText();
    this.c.node('File', fileKey, {
      name: path.posix.basename(rel), path: rel, repo: this.repo, kind: isTest ? 'test' : 'source',
      lang: isSvelte ? 'svelte' : 'ts', lines: text.split('\n').length, layer,
    });
    this.c.edge('CONTAINS', ['Package', pkgKey], ['File', fileKey], this.src.props(rel, 1));

    const unit = { sf, rel, real, isSvelte, isTest, layer, fileKey, fileSymbol: null, decls: [], typedVars: new Map() };
    const line = (pos) => sf.getLineAndCharacterOfPosition(pos).line + 1;

    // A component, page or test file is itself a symbol that owns the top-level code.
    if (isSvelte || isTest) {
      let kind = isTest ? 'test' : 'component';
      const base = path.posix.basename(rel);
      if (!isTest && /^\+page(\.|$)/.test(base)) kind = 'page';
      if (!isTest && /^\+layout(\.|$)/.test(base)) kind = 'layout';
      const key = `${this.repo}:${rel}`;
      this.c.node('Symbol', key, {
        name: base, fqn: rel, kind, repo: this.repo, file: rel, line_start: 1, line_end: text.split('\n').length,
        exported: true, tokens: tokens(base.replace(/\.(svelte|ts|js)$/, '')), layer, test_file: isTest, lang: isSvelte ? 'svelte' : 'ts',
        doc: isSvelte ? this.svelteDoc(real) : undefined,
      });
      this.c.edge('CONTAINS', ['File', fileKey], ['Symbol', key], this.src.props(rel, 1));
      unit.fileSymbol = this.register(key, { kind, name: base, rel, line: 1, end: text.split('\n').length, isTest, node: sf, nameStart: -1 });
      this.routeFor(unit, kind);
    } else if (/^\+(page|layout|server)(\.server)?\.[jt]s$/.test(path.posix.basename(rel))) {
      this.routeFor(unit, 'load');
    }

    const owner = unit.fileSymbol ? unit.fileSymbol.key : null;
    for (const st of sf.statements) {
      this.declareStatement(unit, st, owner, line);
    }
    return unit;
  }

  svelteDoc(real) {
    const txt = fs.readFileSync(real, 'utf8');
    const m = txt.match(/^\s*<!--([\s\S]*?)-->/);
    return m ? m[1].trim().slice(0, 1000) : undefined;
  }

  register(key, info) {
    const s = { key, ...info };
    this.symbols.set(key, s);
    if (info.nameStart >= 0) this.byPos.set(`${info.rel}:${info.nameStart}`, key);
    return s;
  }

  addSymbol(unit, node, nameNode, ident, kind, owner, extra = {}) {
    const { sf, rel, layer, fileKey, isTest } = unit;
    const start = sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1;
    const end = sf.getLineAndCharacterOfPosition(node.getEnd()).line + 1;
    const name = extra.name || ident;
    const key = `${this.repo}:${rel}#${name}`;
    const exported = !!(ts.getCombinedModifierFlags(node) & ts.ModifierFlags.Export) || !!owner;
    this.c.node('Symbol', key, {
      name, fqn: `${rel}#${name}`, kind, repo: this.repo, file: rel, line_start: start, line_end: end, exported,
      signature: extra.signature, doc: this.docOf(unit, node), tokens: tokens(ident), layer, test_file: isTest, lang: unit.isSvelte ? 'svelte' : 'ts',
      json_fields: extra.json_fields, fields: extra.fields,
    });
    this.c.edge('CONTAINS', owner ? ['Symbol', owner] : ['File', fileKey], ['Symbol', key], this.src.props(rel, start));
    const nameStart = nameNode ? nameNode.getStart(sf) : node.getStart(sf);
    return this.register(key, { kind, name, rel, line: start, end, isTest, node, nameStart, typeName: extra.typeName });
  }

  docOf(unit, node) {
    const text = unit.sf.getFullText();
    const ranges = ts.getLeadingCommentRanges(text, node.getFullStart()) || [];
    if (!ranges.length) return undefined;
    const r = ranges[ranges.length - 1];
    return text.slice(r.pos, r.end).replace(/^\/\*\*?|\*\/$/g, '').replace(/^\s*(\*|\/\/)\s?/gm, '').trim().slice(0, 1000) || undefined;
  }

  declareStatement(unit, st, owner, line) {
    const { sf } = unit;
    if (ts.isFunctionDeclaration(st) && st.name) {
      this.addSymbol(unit, st, st.name, st.name.text, 'function', owner, { signature: this.sig(st) });
    } else if (ts.isClassDeclaration(st) && st.name) {
      const cls = this.addSymbol(unit, st, st.name, st.name.text, 'class', owner);
      for (const m of st.members) {
        if ((ts.isMethodDeclaration(m) || ts.isConstructorDeclaration(m)) && m.body) {
          const mname = ts.isConstructorDeclaration(m) ? 'constructor' : m.name.getText(sf);
          this.addSymbol(unit, m, ts.isConstructorDeclaration(m) ? null : m.name, mname, 'method', cls.key, { name: `${st.name.text}.${mname}`, signature: this.sig(m) });
        }
      }
    } else if (ts.isInterfaceDeclaration(st) || ts.isTypeAliasDeclaration(st)) {
      const fields = ts.isInterfaceDeclaration(st)
        ? st.members.filter((m) => ts.isPropertySignature(m) && m.name).map((m) => m.name.getText(sf))
        : (ts.isTypeLiteralNode(st.type) ? st.type.members.filter((m) => ts.isPropertySignature(m) && m.name).map((m) => m.name.getText(sf)) : []);
      this.addSymbol(unit, st, st.name, st.name.text, 'type', owner, { fields: fields.length ? fields : undefined, json_fields: fields.length ? fields : undefined, typeName: st.name.text });
    } else if (ts.isEnumDeclaration(st)) {
      this.addSymbol(unit, st, st.name, st.name.text, 'type', owner, { typeName: st.name.text });
    } else if (ts.isVariableStatement(st)) {
      for (const d of st.declarationList.declarations) {
        if (ts.isIdentifier(d.name)) {
          const init = d.initializer;
          if (init && (ts.isArrowFunction(init) || ts.isFunctionExpression(init))) {
            this.addSymbol(unit, d, d.name, d.name.text, 'function', owner, { signature: this.sig(init) });
          } else if (init && ts.isObjectLiteralExpression(init)) {
            const obj = this.addSymbol(unit, d, d.name, d.name.text, 'const', owner);
            for (const p of init.properties) {
              const fn = ts.isPropertyAssignment(p) ? p.initializer : (ts.isMethodDeclaration(p) ? p : null);
              if (fn && (ts.isArrowFunction(fn) || ts.isFunctionExpression(fn) || ts.isMethodDeclaration(fn)) && p.name) {
                const pname = p.name.getText(sf);
                this.addSymbol(unit, p, p.name, pname, d.name.text === 'api' ? 'api_method' : 'method', obj.key, { name: `${d.name.text}.${pname}`, signature: this.sig(fn) });
              }
            }
          } else {
            const kind = st.declarationList.flags & ts.NodeFlags.Const ? 'const' : 'var';
            this.addSymbol(unit, d, d.name, d.name.text, kind, owner, { signature: d.type ? d.type.getText(sf) : undefined });
            this.noteTypedVar(unit, d.name.text, d.type);
          }
        } else if (ts.isObjectBindingPattern(d.name) && d.type && ts.isTypeLiteralNode(d.type)) {
          // Svelte 5: let { receipt }: { receipt: Receipt } = $props()
          for (const m of d.type.members) {
            if (ts.isPropertySignature(m) && m.name) this.noteTypedVar(unit, m.name.getText(sf), m.type);
          }
        }
      }
    }
  }

  noteTypedVar(unit, name, typeNode) {
    if (!typeNode) return;
    const t = typeNode.getText(unit.sf).replace(/\[\]$/, '').replace(/^Array<(.+)>$/, '$1').trim();
    if (/^[A-Z][A-Za-z0-9_]*$/.test(t)) unit.typedVars.set(name, t);
  }

  sig(fn) {
    const sf = fn.getSourceFile();
    const params = fn.parameters ? fn.parameters.map((p) => p.getText(sf)).join(', ') : '';
    const ret = fn.type ? `: ${fn.type.getText(sf)}` : '';
    return `(${params})${ret}`.slice(0, 300);
  }

  routeFor(unit, kind) {
    const m = unit.rel.match(/^src\/routes(\/.*)?\/\+(page|layout|server)/);
    if (!m) return;
    let route = (m[1] || '/').replace(/\/\([^)]*\)/g, '') || '/';
    const key = `${this.repo}:${route}`;
    const params = [...route.matchAll(/\[\.{0,3}([^\]=]+)(?:=[^\]]+)?\]/g)].map((x) => x[1]);
    this.c.node('UiRoute', key, { name: route, path: route, repo: this.repo, owner_repo: this.repo, params: params.length ? params : undefined, kind: m[2] === 'server' ? 'endpoint' : m[2], source_ref: this.src.ref(unit.rel, 1) });
    this.routes.set(normalizePath(route.replace(/\[\.{0,3}[^\]]+\]/g, '{}')), key);
    const handler = unit.fileSymbol ? unit.fileSymbol.key : null;
    if (handler) this.c.edge('HANDLES', ['Symbol', handler], ['UiRoute', key], this.src.props(unit.rel, 1, { kind }));
    else unit.pendingRoute = key; // +page.ts: linked to its load() once declared
  }

  // ---- pass 2 --------------------------------------------------------------

  link(unit) {
    const { sf, rel } = unit;
    const line = (pos) => sf.getLineAndCharacterOfPosition(pos).line + 1;
    if (unit.pendingRoute) {
      const load = this.symbols.get(`${this.repo}:${rel}#load`) || this.symbols.get(`${this.repo}:${rel}#GET`);
      if (load) this.c.edge('HANDLES', ['Symbol', load.key], ['UiRoute', unit.pendingRoute], this.src.props(rel, load.line, { kind: 'load' }));
    }
    // imports
    for (const st of sf.statements) {
      if (ts.isImportDeclaration(st) && ts.isStringLiteral(st.moduleSpecifier)) {
        const target = this.resolveImport(unit, st.moduleSpecifier.text);
        if (target) {
          this.c.edge('IMPORTS', ['File', unit.fileKey], ['File', this.fileKey(target)], this.src.props(rel, line(st.getStart(sf))));
          if (target.endsWith('.svelte') && unit.fileSymbol) {
            this.c.edge('REFERENCES', ['Symbol', unit.fileSymbol.key], ['Symbol', `${this.repo}:${target}`], this.src.props(rel, line(st.getStart(sf)), { how: 'component' }));
          }
        }
      }
    }
    // mirrored types
    if (this.schemas) {
      for (const s of this.symbols.values()) {
        if (s.rel !== rel || s.kind !== 'type' || !s.typeName) continue;
        const fields = this.schemas.schemas[s.typeName];
        if (!fields) continue;
        this.mirrored.set(s.typeName, s.typeName);
        const node = this.c.nodes.find((n) => n.label === 'Symbol' && n.key === s.key);
        const have = new Set(node?.props.fields || []);
        const want = new Set(fields);
        this.c.edge('MIRRORS', ['Symbol', s.key], ['Schema', `${this.schemas.owner}:${s.typeName}`], this.src.props(rel, s.line, {
          match: 'name',
          missing_in_code: [...want].filter((f) => !have.has(f)).sort(),
          missing_in_spec: [...have].filter((f) => !want.has(f)).sort(),
        }));
      }
    }
    // calls, endpoints, field access
    const owners = [...this.symbols.values()].filter((s) => s.rel === rel && s.nameStart >= 0).sort((a, b) => a.node.getStart(sf) - b.node.getStart(sf));
    const ownerAt = (pos) => {
      let best = unit.fileSymbol || null;
      for (const s of owners) {
        if (s.node.getStart(sf) <= pos && pos < s.node.getEnd()) best = s;
      }
      return best;
    };
    const seenRef = new Set();
    const visit = (node) => {
      const from = ownerAt(node.getStart(sf));
      if (from) {
        if (ts.isCallExpression(node) || ts.isNewExpression(node)) this.call(unit, node, from, line);
        if (ts.isPropertyAccessExpression(node)) this.fieldAccess(unit, node, from, line);
        if (ts.isTypeReferenceNode(node) && ts.isIdentifier(node.typeName)) this.typeRef(unit, node, from, line, seenRef);
        if (ts.isIdentifier(node) && !ts.isPropertyAccessExpression(node.parent) && !ts.isCallExpression(node.parent)) this.valueRef(unit, node, from, line);
      }
      ts.forEachChild(node, visit);
    };
    visit(sf);
    if (unit.isSvelte) this.markupFields(unit, line);
  }

  resolveImport(unit, spec) {
    if (spec.endsWith('.svelte') || spec.endsWith('.svelte.ts')) {
      let base = spec.startsWith('$lib') ? path.join(this.root, 'src/lib', spec.slice(4)) : (spec.startsWith('.') ? path.resolve(path.dirname(unit.real), spec) : null);
      if (!base) return null;
      base = base.replace(/\.svelte\.ts$/, '.svelte');
      return fs.existsSync(base) ? this.rel(base) : null;
    }
    const r = ts.resolveModuleName(spec, unit.sf.fileName, this.options, this.host);
    const f = r.resolvedModule?.resolvedFileName;
    if (!f || f.includes('/node_modules/') || !f.startsWith(this.root + path.sep)) return null;
    return this.rel(f);
  }

  targetKey(symbol) {
    if (!symbol) return null;
    let s = symbol;
    if (s.flags & ts.SymbolFlags.Alias) {
      try { s = this.checker.getAliasedSymbol(s); } catch { return null; }
    }
    for (const d of s.declarations || []) {
      const dsf = d.getSourceFile();
      const real = this.svelteOf.get(dsf.fileName) || dsf.fileName;
      if (!real.startsWith(this.root + path.sep)) continue;
      const rel = this.rel(real);
      const nameNode = d.name || (ts.isConstructorDeclaration(d) ? null : null);
      const start = nameNode ? nameNode.getStart(dsf) : d.getStart(dsf);
      const key = this.byPos.get(`${rel}:${start}`);
      if (key) return key;
      // a method of an object literal: byPos is keyed on the property name
      if (ts.isPropertyAssignment(d) || ts.isMethodDeclaration(d)) {
        const k2 = this.byPos.get(`${rel}:${d.name.getStart(dsf)}`);
        if (k2) return k2;
      }
    }
    return null;
  }

  call(unit, node, from, line) {
    const { sf, rel } = unit;
    const callee = node.expression;
    const ln = line(node.getStart(sf));
    let nameNode = callee;
    if (ts.isPropertyAccessExpression(callee)) nameNode = callee.name;
    const calleeText = callee.getText(sf);
    const calleeName = ts.isPropertyAccessExpression(callee) ? callee.name.text : (ts.isIdentifier(callee) ? callee.text : '');

    // Static call edge.
    const key = this.targetKey(this.checker.getSymbolAtLocation(nameNode));
    if (key && key !== from.key) {
      this.c.edge('CALLS', ['Symbol', from.key], ['Symbol', key], this.src.props(rel, ln, { via: 'static' }));
      if (from.isTest) this.testCalls.push([from, key, ln, rel]);
    }

    // HTTP client calls.
    const args = node.arguments || [];
    const url = (expr) => this.pathOf(unit, expr);
    if (ts.isNewExpression(node) && calleeText === 'EventSource' && args[0]) {
      const p = url(args[0]);
      if (p) this.endpoint(from, 'GET', p, 'event_source', rel, ln);
      return;
    }
    if (/^(request|fetch|fetchOrNetworkError|apiFetch|http|client)$/.test(calleeName) && args[0]) {
      const p = url(args[0]);
      if (!p) return;
      let method = 'GET';
      if (args[1] && ts.isObjectLiteralExpression(args[1])) {
        for (const prop of args[1].properties) {
          if (ts.isPropertyAssignment(prop) && prop.name.getText(sf) === 'method' && ts.isStringLiteralLike(prop.initializer)) method = prop.initializer.text.toUpperCase();
        }
      }
      const base = args[2] ? args[2].getText(sf) : '';
      const prefix = /AUTH|auth/.test(base) ? '' : this.apiPrefix;
      this.endpoint(from, method, (p.startsWith('/api/') ? '' : prefix) + p, 'fetch', rel, ln);
      return;
    }
    if (calleeName === 'open' && args.length >= 2 && ts.isStringLiteralLike(args[0]) && /^(GET|POST|PUT|PATCH|DELETE)$/i.test(args[0].text)) {
      const p = url(args[1]);
      if (p) this.endpoint(from, args[0].text.toUpperCase(), p, 'xhr', rel, ln);
      return;
    }
    // Playwright.
    if (from.isTest && ts.isPropertyAccessExpression(callee) && args[0]) {
      if (calleeName === 'route') this.mock(from, args[0], rel, ln, sf);
      if (calleeName === 'goto') this.visit(from, args[0], rel, ln, sf);
    }
  }

  // Path of a URL expression: template literals keep their text with ${…} → {}; a
  // leading ${BASE}-style prefix is dropped (the prefix is added back by the caller).
  pathOf(unit, expr) {
    const sf = unit.sf;
    let text = null;
    if (ts.isStringLiteralLike(expr)) text = expr.text;
    else if (ts.isTemplateExpression(expr)) {
      text = expr.head.text;
      for (const span of expr.templateSpans) text += `\${${span.expression.getText(sf)}}` + span.literal.text;
    } else if (ts.isBinaryExpression(expr) && expr.operatorToken.kind === ts.SyntaxKind.PlusToken) {
      const l = this.pathOf(unit, expr.left) || '';
      const r = this.pathOf(unit, expr.right) || '';
      text = l + r;
    } else if (ts.isIdentifier(expr)) {
      // const url = `${BASE}/receipts/${id}/events`; new EventSource(url)
      const sym = this.checker.getSymbolAtLocation(expr);
      const d = sym?.valueDeclaration;
      if (d && ts.isVariableDeclaration(d) && d.initializer) return this.pathOf(unit, d.initializer);
      return null;
    } else return null;
    if (text === null) return null;
    text = text.replace(/^\$\{[^}]*(BASE|base|Base|URL|url)[^}]*\}/, '');
    if (!text.startsWith('/')) return null;
    return text.split('?')[0];
  }

  endpoint(from, method, p, how, rel, ln) {
    const key = endpointKey(method, p);
    this.c.node('HttpEndpoint', key, { name: key, method: method.toUpperCase(), path_pattern: normalizePath(p), path_code: undefined, in_client: true });
    this.c.edge('CALLS_ENDPOINT', ['Symbol', from.key], ['HttpEndpoint', key], this.src.props(rel, ln, { method: method.toUpperCase(), how, client_path: p }));
  }

  mock(from, arg, rel, ln, sf) {
    let pattern = null;
    let prefix = false;
    if (ts.isStringLiteralLike(arg) || ts.isTemplateExpression(arg)) {
      let t = ts.isStringLiteralLike(arg) ? arg.text : this.templateText(arg, sf);
      const i = t.indexOf('/api/');
      if (i < 0) return;
      t = t.slice(i);
      if (/\*\*$/.test(t)) { prefix = true; t = t.replace(/\/?\*\*$/, ''); }
      pattern = normalizePath(t.replace(/\*\*/g, '{}').replace(/\*/g, '{}'));
      if (pattern.endsWith('/{}') && prefix) pattern = pattern.slice(0, -3);
    } else if (ts.isNewExpression(arg) && arg.expression.getText(sf) === 'RegExp' && arg.arguments?.[0]) {
      const a0 = arg.arguments[0];
      let t = ts.isStringLiteralLike(a0) ? a0.text : (ts.isTemplateExpression(a0) ? this.templateText(a0, sf) : null);
      if (!t) return;
      const i = t.indexOf('/api/');
      if (i < 0) return;
      t = t.slice(i).replace(/\\\//g, '/').replace(/\(\/\.\*\)\?/g, '').replace(/[$^]/g, '').replace(/\\\./g, '.').replace(/\[[^\]]*\][+*]?/g, '{}').replace(/\.\*/g, '{}');
      pattern = normalizePath(t);
    } else return;
    const key = `ANY ${pattern}`;
    this.c.node('HttpEndpoint', key, { name: key, method: 'ANY', path_pattern: pattern, placeholder: true });
    this.c.edge('MOCKS', ['Symbol', from.key], ['HttpEndpoint', key], this.src.props(rel, ln, { pattern: arg.getText(sf).slice(0, 120), prefix: prefix || undefined }));
  }

  visit(from, arg, rel, ln, sf) {
    let t = ts.isStringLiteralLike(arg) ? arg.text : (ts.isTemplateExpression(arg) ? this.templateText(arg, sf) : null);
    if (!t || !t.startsWith('/')) return;
    const pat = normalizePath(t.split('?')[0]);
    const route = this.routes.get(pat);
    if (route) this.c.edge('VISITS', ['Symbol', from.key], ['UiRoute', route], this.src.props(rel, ln, { path: t }));
  }

  templateText(expr, sf) {
    let text = expr.head.text;
    for (const span of expr.templateSpans) text += `\${${span.expression.getText(sf)}}` + span.literal.text;
    return text;
  }

  schemaOfType(type) {
    if (!this.schemas) return null;
    const names = [];
    const push = (t) => { const n = t.aliasSymbol?.name || t.symbol?.name; if (n) names.push(n); };
    if (type.isUnion && type.isUnion()) for (const t of type.types) push(t); else push(type);
    for (const n of names) if (this.schemas.schemas[n]) return n;
    return null;
  }

  fieldAccess(unit, node, from, line) {
    if (!this.schemas) return;
    const { sf, rel } = unit;
    let type;
    try { type = this.checker.getTypeAtLocation(node.expression); } catch { return; }
    const schema = this.schemaOfType(type);
    if (!schema) return;
    const field = node.name.text;
    if (!this.schemas.schemas[schema].includes(field)) return;
    const write = ts.isBinaryExpression(node.parent) && node.parent.left === node && node.parent.operatorToken.kind === ts.SyntaxKind.EqualsToken;
    this.c.edge('USES_FIELD', ['Symbol', from.key], ['Field', `${this.schemas.owner}:${schema}.${field}`], this.src.props(rel, line(node.getStart(sf)), { access: write ? 'write' : 'read', via: 'script' }));
  }

  // Field reads inside Svelte markup, resolved through the declared types of script variables.
  markupFields(unit, line) {
    if (!this.schemas || unit.typedVars.size === 0) return;
    const text = svelteMarkupOnly(fs.readFileSync(unit.real, 'utf8'));
    const re = /\b([A-Za-z_$][\w$]*)\.([a-z_][\w]*)\b/g;
    let m;
    const seen = new Set();
    while ((m = re.exec(text)) !== null) {
      const typeName = unit.typedVars.get(m[1]);
      if (!typeName || !this.schemas.schemas[typeName] || !this.schemas.schemas[typeName].includes(m[2])) continue;
      const ln = text.slice(0, m.index).split('\n').length;
      const k = `${typeName}.${m[2]}:${ln}`;
      if (seen.has(k)) continue;
      seen.add(k);
      const from = unit.fileSymbol ? unit.fileSymbol.key : null;
      if (!from) return;
      this.c.edge('USES_FIELD', ['Symbol', from], ['Field', `${this.schemas.owner}:${typeName}.${m[2]}`], {
        ...this.src.props(unit.rel, ln, { access: 'read', via: 'markup' }), confidence: 0.8,
      });
    }
  }

  typeRef(unit, node, from, line, seen) {
    const key = this.targetKey(this.checker.getSymbolAtLocation(node.typeName));
    if (!key || key === from.key) return;
    let how = 'uses';
    for (let p = node.parent; p; p = p.parent) {
      if (ts.isParameter(p)) { how = 'param'; break; }
      if ((ts.isFunctionLike(p)) && p.type && p.type.pos <= node.pos && node.end <= p.type.end) { how = 'return'; break; }
      if (ts.isPropertySignature(p) || ts.isPropertyDeclaration(p)) { how = 'field'; break; }
    }
    const k = `${from.key}|${key}|${how}`;
    if (seen.has(k)) return;
    seen.add(k);
    this.c.edge('REFERENCES', ['Symbol', from.key], ['Symbol', key], this.src.props(unit.rel, line(node.getStart(unit.sf)), { how }));
  }

  // A function used as a value (callback, store subscription) is a potential call.
  valueRef(unit, node, from, line) {
    if (ts.isImportSpecifier(node.parent) || ts.isImportClause(node.parent) || ts.isExportSpecifier(node.parent)) return;
    if (ts.isVariableDeclaration(node.parent) && node.parent.name === node) return;
    if (ts.isParameter(node.parent) || ts.isPropertyAssignment(node.parent) && node.parent.name === node) return;
    const sym = this.checker.getSymbolAtLocation(node);
    if (!sym) return;
    const key = this.targetKey(sym);
    if (!key || key === from.key) return;
    const target = this.symbols.get(key);
    if (!target || !/^(function|method|api_method)$/.test(target.kind)) return;
    this.c.edge('CALLS', ['Symbol', from.key], ['Symbol', key], this.src.props(unit.rel, line(node.getStart(unit.sf)), { via: 'value' }));
  }

  tests() {
    const seen = new Set();
    for (const [from, key, ln, rel] of this.testCalls) {
      const t = this.symbols.get(key);
      if (!t || t.isTest || seen.has(`${from.key}|${key}`)) continue;
      seen.add(`${from.key}|${key}`);
      this.c.edge('TESTS', ['Symbol', from.key], ['Symbol', key], this.src.props(rel, ln, { how: 'calls' }));
    }
  }
}

// ---------------------------------------------------------------- main

const args = parseArgs(process.argv.slice(2));
const started = Date.now();
const x = new Extractor(args);
const claims = x.run();
if (args.verbose) {
  console.error(claims.stats());
  console.error(`done in ${Date.now() - started}ms`);
}
const json = JSON.stringify({ nodes: claims.nodes, edges: claims.edges });
if (args.out === '-') process.stdout.write(json);
else fs.writeFileSync(args.out, json);
