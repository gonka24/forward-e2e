// No npm dependencies. Run the actual compiled CosmWasm ExternalQuerier in Wasm.
// Host injection is synthetic: this proves the Rust query_chain ABI decoder,
// not that a healthy Gonka keeper can emit every SystemError variant.
import fs from 'node:fs';
import crypto from 'node:crypto';
import assert from 'node:assert/strict';

const [wasmPath, output] = process.argv.slice(2);
if (!wasmPath || !output) throw new Error('Usage: node test_wasm_query_boundary.mjs probe.wasm evidence.json');
const wasm = fs.readFileSync(wasmPath);
const module = await WebAssembly.compile(wasm);
const cases = [
  ['malformed-envelope', '{', 'invalid_response'],
  ['invalid-envelope-shape', '{"unexpected":true}', 'invalid_response'],
  ...['invalid_request', 'unknown', 'no_such_contract', 'no_such_code', 'unsupported_request'].map(kind => {
    const payload = {invalid_request: {error: 'bad request', request: 'e30='}, unknown: {},
      no_such_contract: {addr: 'missing'}, no_such_code: {code_id: 7},
      unsupported_request: {kind: 'grpc'}}[kind];
    return [kind, JSON.stringify({error: {[kind]: payload}}), kind];
  }),
  ['contract-error-control', '{"ok":{"error":"keeper failure"}}', null],
  ['success-control', '{"ok":{"ok":"AQI="}}', null],
];
const evidence = {level: 'compiled-Wasm query_chain ABI; synthetic host',
  wasm_sha256: crypto.createHash('sha256').update(wasm).digest('hex'), node: process.version,
  status: 'RUNNING', cases: []};
const persist = () => fs.writeFileSync(output, JSON.stringify(evidence, null, 2) + '\n');
persist();
try {
  for (const [name, envelope, expectedKind] of cases) {
    let instance;
    let calls = 0;
    const read = ptr => {
      const view = new DataView(instance.exports.memory.buffer);
      const offset = view.getUint32(ptr, true), length = view.getUint32(ptr + 8, true);
      return Buffer.from(new Uint8Array(instance.exports.memory.buffer, offset, length));
    };
    const write = bytes => {
      const ptr = instance.exports.allocate(bytes.length);
      const view = new DataView(instance.exports.memory.buffer);
      new Uint8Array(instance.exports.memory.buffer, view.getUint32(ptr, true), bytes.length).set(bytes);
      view.setUint32(ptr + 8, bytes.length, true);
      return ptr;
    };
    const request = Buffer.from('{"grpc":{"path":"/inference.inference.Query/GetCurrentEpoch","data":""}}');
    const imports = {};
    for (const imp of WebAssembly.Module.imports(module)) {
      assert.equal(imp.kind, 'function');
      (imports[imp.module] ??= {})[imp.name] = (...args) => {
        if (imp.name === 'query_chain') {
          calls++;
          assert.deepEqual(read(args[0]), request);
          return write(Buffer.from(envelope));
        }
        throw new Error(`Unexpected host call: ${imp.name}`);
      };
    }
    instance = await WebAssembly.instantiate(module, imports);
    const env = {block: {height: 123, time: '123000000000', chain_id: 'abi-test'},
      transaction: null, contract: {address: 'contract'}};
    const resultPtr = instance.exports.query(write(Buffer.from(JSON.stringify(env))),
      write(Buffer.from(JSON.stringify({request: request.toString('base64')}))));
    const outer = JSON.parse(read(resultPtr).toString());
    assert.equal(typeof outer.ok, 'string', JSON.stringify(outer));
    const result = JSON.parse(Buffer.from(outer.ok, 'base64').toString());
    assert.equal(calls, 1);
    if (expectedKind) {
      assert.deepEqual(Object.keys(result.error), [expectedKind]);
      if (expectedKind === 'invalid_response') {
        assert.equal(result.error.invalid_response.response, Buffer.from(envelope).toString('base64'));
        assert.ok(result.error.invalid_response.error.length > 0);
      } else assert.deepEqual(result, JSON.parse(envelope));
    } else assert.deepEqual(result, JSON.parse(envelope));
    evidence.cases.push({name, status: 'PASS', envelope, observed: result, query_chain_calls: calls});
    persist();
  }
  evidence.status = 'PASS';
} catch (error) {
  evidence.status = 'FAIL'; evidence.error = String(error); throw error;
} finally { persist(); }
