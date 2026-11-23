import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';
import ts from 'typescript';

const source = readFileSync(new URL('../src/reexecution-config.ts', import.meta.url), 'utf8');
const compiled = ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText;
const check = (config) => runInNewContext(`${compiled}\nexports.validateReplayConfig(input)`,
  { input: config, exports: {} });

const recorded = { agentVersion: 'sha256:abc', mode: 'recorded', liveAuthorized: false,
  brokerReady: false, runnerReady: true, maxModelCalls: 20 };

test('recorded mode labels mocked tools and live needs both approvals', () => {
  assert.match(check(recorded), /production tools remain mocked/);
  assert.throws(() => check({ ...recorded, mode: 'live' }), /explicit authorization/);
  assert.throws(() => check({ ...recorded, mode: 'live', liveAuthorized: true }), /verified broker/);
  assert.match(check({ ...recorded, mode: 'live', liveAuthorized: true, brokerReady: true }), /production tools remain mocked/);
});

test('unavailable runner and over-budget calls fail closed', () => {
  assert.throws(() => check({ ...recorded, runnerReady: false }), /runner is unavailable/);
  assert.throws(() => check({ ...recorded, maxModelCalls: 21 }), /budget exceeds policy/);
});

test('truthy nonboolean approvals cannot enable live inference', () => {
  const approved = { ...recorded, mode: 'live', liveAuthorized: true, brokerReady: true };
  assert.throws(() => check({ ...approved, liveAuthorized: 'yes' }), /explicit authorization/);
  assert.throws(() => check({ ...approved, brokerReady: 1 }), /verified broker/);
  assert.throws(() => check({ ...approved, runnerReady: 'yes' }), /runner is unavailable/);
});
