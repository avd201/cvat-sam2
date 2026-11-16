// Runs the REAL service.ts (bundled with esbuild) against a mocked fetch() that streams NDJSON.
// Skipped automatically when esbuild cannot be fetched (offline machine).
import test from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
let svc = null;
let skipReason = null;
try {
    const out = join(mkdtempSync(join(tmpdir(), 'sam2-svc-')), 'service.mjs');
    execFileSync('npx', ['--yes', 'esbuild', join(here, '../src/ts/service.ts'), '--bundle', '--format=esm',
        '--platform=node', `--outfile=${out}`, '--log-level=error'], { stdio: 'pipe' });
    svc = await import(pathToFileURL(out).href);
} catch (e) {
    skipReason = `esbuild unavailable: ${e.message.split('\n')[0]}`;
}

const enc = new TextEncoder();
function mockFetch(chunks, status = 200) {
    globalThis.fetch = async () => ({
        ok: status === 200,
        status,
        text: async () => chunks.join(''),
        body: new ReadableStream({
            start(controller) {
                chunks.forEach((c) => controller.enqueue(enc.encode(c)));
                controller.close();
            },
        }),
    });
}
const body = { job_id: 1, org: '', from_frame: 0, to_frame: 2, prompt_type: 'polygon', prompt_points: [0, 0, 1, 0, 1, 1], output: 'polygon', simplify_px: 1.5 };

test('queue lines are reported, never mistaken for results, and the run still completes', { skip: skipReason ?? false }, async () => {
    mockFetch([
        '{"queued":2}\n{"que', 'ued":1}\n',
        '{"frame":0,"points":[0,0,1,0,1,1]}\n{"frame":1,"points":null}\n',
        '{"done":true,"frames":2,"output":"polygon"}\n',
    ]);
    const queued = [];
    const frames = [];
    const res = await svc.runPropagation(body, (n) => frames.push(n), new AbortController().signal, (a) => queued.push(a));
    assert.deepEqual(queued, [2, 1]);
    assert.deepEqual(frames, [1, 2]);
    assert.equal(res.size, 2);
});

test('an old caller that passes no onQueued callback still works', { skip: skipReason ?? false }, async () => {
    mockFetch(['{"queued":1}\n{"frame":0,"points":[0,0,1,0,1,1]}\n{"done":true,"frames":1,"output":"polygon"}\n']);
    const res = await svc.runPropagation(body, () => {}, new AbortController().signal);
    assert.equal(res.size, 1);
});

test('a service error line after waiting in the queue is surfaced with its text (e.g. out of memory)', { skip: skipReason ?? false }, async () => {
    mockFetch(['{"queued":1}\n{"error":"The GPU ran out of memory while tracking 3 object(s) over 100 frames."}\n']);
    await assert.rejects(
        svc.runPropagation(body, () => {}, new AbortController().signal, () => {}),
        /ran out of memory/,
    );
});

test('a stream that ends while still queued is an error, not a silent success', { skip: skipReason ?? false }, async () => {
    mockFetch(['{"queued":3}\n']);
    await assert.rejects(
        svc.runPropagation(body, () => {}, new AbortController().signal, () => {}),
        /ended without completing/,
    );
});

test('the final line is handed to onDone, including which objects left the region of interest', { skip: skipReason ?? false }, async () => {
    mockFetch([
        '{"frame":0,"points":[0,0,1,0,1,1]}\n{"frame":1,"points":null}\n{"frame":2,"points":null}\n',
        '{"done":true,"frames":3,"output":"polygon","dropped":{"1":1}}\n',
    ]);
    let final = null;
    const res = await svc.runPropagation(body, () => {}, new AbortController().signal, undefined, (m) => { final = m; });
    assert.deepEqual(final.dropped, { 1: 1 });
    assert.deepEqual([...res.keys()], [0, 1, 2]);
    assert.equal(res.get(1), null);                       // dropped frames arrive as "no result" and become outside markers
});

test('multi-object: onDone also fires and every object keeps its null frames', { skip: skipReason ?? false }, async () => {
    mockFetch([
        '{"frame":0,"obj":1,"points":[0,0,1,0,1,1]}\n{"frame":0,"obj":2,"points":[0,0,1,0,1,1]}\n',
        '{"frame":1,"obj":1,"points":null}\n{"frame":1,"obj":2,"points":[0,0,1,0,1,1]}\n',
        '{"done":true,"frames":2,"objects":2,"output":"polygon","dropped":{"1":1}}\n',
    ]);
    const mbody = { ...body, prompt_type: undefined, prompt_points: undefined, objects: [{ id: 1 }, { id: 2 }] };
    let final = null;
    const res = await svc.runPropagationMulti(mbody, () => {}, new AbortController().signal, undefined, (m) => { final = m; });
    assert.deepEqual(final.dropped, { 1: 1 });
    assert.equal(res.get(1).get(1), null);
    assert.notEqual(res.get(2).get(1), null);
});

const roiBody = { ...body, roi_lines: [[0, 0, 10, 0], [5, 0, 5, 9, 9, 3]] };

test('a service that echoes the number of ROI lines it applied is accepted', { skip: skipReason ?? false }, async () => {
    mockFetch(['{"frame":0,"points":[0,0,1,0,1,1]}\n{"done":true,"frames":1,"output":"polygon","roi_lines":2}\n']);
    const res = await svc.runPropagation(roiBody, () => {}, new AbortController().signal);
    assert.equal(res.size, 1);
});

test('a service that did not apply the ROI (older build, or a different count) is refused', { skip: skipReason ?? false }, async () => {
    for (const done of ['{"done":true,"frames":1,"output":"polygon"}', '{"done":true,"frames":1,"output":"polygon","roi_lines":1}']) {
        mockFetch([`{"frame":0,"points":[0,0,1,0,1,1]}\n${done}\n`]);
        await assert.rejects(svc.runPropagation(roiBody, () => {}, new AbortController().signal), /did not apply it; the service is outdated/);
    }
});
