// Offline ECMAScript reference for the shared RFC 8785 fixture corpus.
const fs = require("node:fs");
function canonical(value) {
  if (Array.isArray(value)) return "[" + value.map(canonical).join(",") + "]";
  if (value !== null && typeof value === "object") {
    return "{" + Object.keys(value).sort().map(key =>
      JSON.stringify(key) + ":" + canonical(value[key])).join(",") + "}";
  }
  return JSON.stringify(value);
}
const fixtures = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
for (const fixture of fixtures) {
  if (canonical(fixture.value) !== fixture.canonical) {
    throw new Error("Canonical mismatch: " + fixture.name);
  }
}
process.stdout.write(JSON.stringify(fixtures.map(fixture => canonical(fixture.value))));
