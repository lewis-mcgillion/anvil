import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';
import ts from 'typescript';

const source = readFileSync(new URL('../src/divergence-display.ts', import.meta.url), 'utf8');
const compiled = ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText;
const display = (evidence) => runInNewContext(`${compiled}\nexports.divergenceDisplay(input)`,
  { input: evidence, exports: {} });

test('missing branches never imply equality or a judge pass', () => {
  const value = display({ baseline: { arguments: { sku: 'A' } }, alignment: 'ambiguous' });
  assert.equal(value.candidate, 'Evidence missing');
  assert.equal(value.alignment, 'ambiguous');
  assert.equal(value.judge, 'Not judged');
});

test('malicious payload remains inert serialized text', () => {
  const attack = '<img src=x onerror=alert(1)>';
  const value = display({ baseline: { text: attack }, candidate: { text: 'safe' } });
  assert.ok(value.baseline.includes(attack));
  assert.notEqual(value.baseline, value.candidate);
});
