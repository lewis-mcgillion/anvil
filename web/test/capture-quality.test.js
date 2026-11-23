import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';
import ts from 'typescript';

const source = readFileSync(new URL('../src/capture-quality.ts', import.meta.url), 'utf8');
const compiled = ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText;
const quality = (run) => runInNewContext(`${compiled}\nexports.captureQualityLabel(input)`,
  { input: run, exports: {} });

test('capture quality requires explicit complete, succeeded, eligible evidence', () => {
  assert.equal(quality({}), 'Capture incomplete or unknown');
  assert.equal(quality({ capture_status: 'sealed_complete' }), 'Execution failed or unknown');
  assert.equal(quality({ capture_status: 'sealed_complete', execution_outcome: 'succeeded' }),
    'Replay eligibility unverified');
  assert.equal(quality({ capture_status: 'sealed_complete', execution_outcome: 'succeeded',
    replay_eligibility: 'eligible' }), 'Complete, succeeded and replay-eligible');
});

test('negative and contradictory evidence never receives a complete label', () => {
  const complete = 'Complete, succeeded and replay-eligible';
  const base = { capture_status: 'sealed_complete', execution_outcome: 'succeeded',
    replay_eligibility: 'eligible' };
  for (const capture_status of [null, 'open', 'sealed_incomplete', 'failed', '']) {
    assert.notEqual(quality({ ...base, capture_status }), complete);
  }
  for (const execution_outcome of [null, 'failed', 'cancelled', 'interrupted', '']) {
    assert.notEqual(quality({ ...base, execution_outcome }), complete);
  }
  for (const replay_eligibility of [null, 'ineligible', 'unknown', '']) {
    assert.notEqual(quality({ ...base, replay_eligibility }), complete);
  }
});
