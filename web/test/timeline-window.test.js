import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';
import ts from 'typescript';

const source = readFileSync(new URL('../src/timeline-window.ts', import.meta.url), 'utf8');
const compiled = ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText;
const windowFor = (...args) => runInNewContext(`${compiled}\nexports.timelineWindow(...params)`,
  { params: args, exports: {} });

test('10k events render only a bounded viewport', () => {
  const result = windowFor(10000, 250000, 25, 500);
  assert.ok(result.end - result.start <= 36);
  assert.equal(result.paddingTop + result.paddingBottom + (result.end - result.start) * 25, 250000);
});

test('invalid scroll and geometry fail explicitly', () => {
  assert.throws(() => windowFor(10000, -1, 25, 500));
  assert.throws(() => windowFor(10000, 1, 0, 500));
});
