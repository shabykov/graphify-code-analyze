# FalkorDB with a working browser UI.
#
# The 6.0.4 image bundles falkordb-browser 2.7.3 with falkordb-node 6.9.1, which only
# recognises the error text "version mismatch" when its cached schema version is stale.
# The Rust engine in 6.0.4 answers "ERR invalid graph version" instead, so every query
# in the UI fails with that message. falkordb-node 7.0.0 accepts both texts; apply the
# same regex to the bundled client until the browser image ships the newer client.
FROM falkordb/falkordb:6.0.4

RUN sed -i "s#if (first.message === 'version mismatch') {#if (/^(?:ERR )?(?:version mismatch|invalid graph version)\$/.test(first.message)) {#" \
      /var/lib/falkordb/browser/node_modules/falkordb/dist/src/commands/QUERY.js \
 && grep -q 'invalid graph version' /var/lib/falkordb/browser/node_modules/falkordb/dist/src/commands/QUERY.js
