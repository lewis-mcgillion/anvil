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
