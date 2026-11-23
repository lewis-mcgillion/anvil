import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';
import ts from 'typescript';

const source = readFileSync(new URL('../src/destructive-confirm.ts', import.meta.url), 'utf8');
const compiled = ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText;
const check = (...args) => runInNewContext(`${compiled}\nexports.confirmProjectDeletion(...params)`,
  { params: args, exports: {} });
const target = { projectId: 'project-a', targetId: 'recording-1', targetKind: 'recording' };

test('destructive action requires exact target and current project', () => {
  assert.equal(check(target, 'project-a', 'recording:recording-1').targetId, 'recording-1');
  assert.throws(() => check(target, 'project-b', 'recording:recording-1'), /unauthorized project/);
  assert.throws(() => check(target, 'project-a', 'recording:other'), /exact deletion target/);
});
