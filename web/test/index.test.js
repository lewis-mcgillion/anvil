import test from 'node:test';
import assert from 'node:assert/strict';

test('node test runner is available without credentials', () => {
  assert.equal(typeof process.env.ANVIL_API_KEY, 'undefined');
});
