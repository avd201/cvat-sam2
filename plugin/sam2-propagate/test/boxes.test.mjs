import test from 'node:test';
import assert from 'node:assert/strict';
import { register } from 'node:module';
// Resolve the extensionless source import as the UI bundler does, without needing npm or CVAT.
register(`data:text/javascript,${encodeURIComponent(`
export async function resolve(specifier, context, next) {
    if (specifier === './plan' && context.parentURL?.endsWith('/boxes.ts')) specifier = './plan.ts';
    return next(specifier, context);
}`)}`, import.meta.url);
const { tightBox, boxSourceKey, planBoxTracks, importBoxTracks, convertToBoxTracks } = await import('../src/ts/boxes.ts');
const poly = [2.5, 3, 12, 7, 4, 19];
const label = { id: 1, attributes: [{ id: 1, mutable: false }, { id: 2, mutable: true }] };
const state = (extra = {}) => ({
    objectType: 'track', shapeType: 'polygon', clientID: 11, serverID: 31,
    label, points: poly, group: { id: 0 }, outside: false, occluded: false,
    source: 'manual', zOrder: 2, attributes: { 1: 'brown', 2: 'standing' }, keyframes: { first: 2 }, ...extra,
});
const mask = (frame, extra = {}) => state({
    objectType: 'shape', shapeType: 'mask', frame, clientID: 100 + frame,
    points: [0, 4, 5, 6, 6, 7], group: { id: 9 }, ...extra,
});
function jobFor(frames, startFrame = 2, stopFrame = 6) {
    const writes = []; const removals = []; const events = [];
    return { startFrame, stopFrame, writes, removals, events, annotations: {
        get: async (f) => (frames.get(f) ?? []).map((s) => ({ ...s, frame: f })),
        import: async (data) => { writes.push(data); events.push('import'); },
        removeBatch: async (batch) => {
            assert.equal(new Set(batch.map((s) => s.frame)).size, 1, 'delete batches target one frame');
            removals.push(batch); events.push('remove');
            return batch.map((s) => s.clientID);
        },
    } };
}

test('polygon bounds use all fractional coordinates with zero margin', () => {
    assert.deepEqual(tightBox('polygon', poly), [2.5, 3, 12, 19]);
    assert.throws(() => tightBox('polygon', [0, 0, 1, NaN, 2, 3]));
    assert.throws(() => tightBox('polygon', [0, 0, 0, 1, 0, 2]));
});
test('mask bounds enclose pixel edges, including one pixel and disconnected components', () => {
    assert.deepEqual(tightBox('mask', [0, 1, 10, 20, 10, 20]), [10, 20, 11, 21]);
    assert.deepEqual(tightBox('mask', [0, 4, 5, 6, 6, 7]), [5, 6, 7, 8]);
    // Non-tight 4×3 crop: visible pixels at offsets 5,6 and 10.
    assert.deepEqual(tightBox('mask', [5, 2, 3, 1, 1, 10, 20, 13, 22]), [11, 21, 13, 23]);
    assert.deepEqual(tightBox('mask', [3, 3, 6, 0, 0, 3, 2]), [0, 0, 4, 2]);
    for (const p of [[4, 0, 0, 1, 1], [0, 5, 0, 0, 1, 1], [0, 1.5, 0, 0, 0, 0]]) {
        assert.throws(() => tightBox('mask', p));
    }
});
test('RLE bounds agree with brute-force pixel bounds on many small masks', () => {
    for (let bits = 1; bits < 512; bits++) {
        const pixels = Array.from({ length: 9 }, (_, i) => (bits >> i) & 1);
        const rle = []; let current = 0; let count = 0;
        for (const p of pixels) {
            if (p === current) count++; else { rle.push(count); count = 1; current = p; }
        }
        rle.push(count);
        const indices = pixels.flatMap((v, i) => v ? [i] : []);
        assert.deepEqual(tightBox('mask', [...rle, 7, 8, 9, 10]), [
            7 + Math.min(...indices.map((i) => i % 3)), 8 + Math.min(...indices.map((i) => Math.floor(i / 3))),
            8 + Math.max(...indices.map((i) => i % 3)), 9 + Math.max(...indices.map((i) => Math.floor(i / 3))),
        ]);
    }
});
test('polygon tracks sample CVAT states on every frame, preserve gaps, metadata and source', async () => {
    const source = state();
    const snapshot = JSON.stringify(source);
    const frames = new Map([[2, [source]], [3, [state({ points: poly.map((v) => v + 1), occluded: true })]],
        [4, [state({ outside: true })]], [5, [state({ attributes: { 1: 'brown', 2: 'sitting' } })]]]);
    const job = jobFor(frames);
    const tracks = await planBoxTracks(job, [source]);
    assert.equal(job.writes.length, 0, 'planning does not write');
    assert.equal(JSON.stringify(source), snapshot);
    assert.equal(tracks[0].frame, 2);
    assert.deepEqual(tracks[0].shapes.map((s) => [s.frame, s.outside]), [[2, false], [3, false], [4, true], [5, false], [6, true]]);
    assert.deepEqual(tracks[0].shapes[1].points, [3.5, 4, 13, 20]);
    assert.equal(tracks[0].shapes[1].occluded, true);
    assert.deepEqual(tracks[0].attributes, [{ spec_id: 1, value: 'brown' }]);
    assert.deepEqual(tracks[0].shapes[3].attributes, [{ spec_id: 2, value: 'sitting' }]);
    assert.equal(tracks[0].shapes[0].z_order, 2);
    await importBoxTracks(job, tracks);
    assert.equal(job.writes.length, 1);
    assert.equal('id' in job.writes[0].tracks[0], false);
});
test('mask objects use their full group, deduplicate selection and close at first missing frame', async () => {
    const job = jobFor(new Map([[2, [mask(2)]], [4, [mask(4)]], [5, [mask(5)]]]));
    const [track] = await planBoxTracks(job, [mask(4), mask(5)]);
    assert.equal(track.group, 9);
    assert.deepEqual(track.shapes.map((s) => [s.frame, s.outside]), [[2, false], [3, true], [4, false], [5, false], [6, true]]);
});
test('ungrouped masks produce one-frame tracks and visibility lasts through the final job frame', async () => {
    const single = mask(6, { group: { id: 0 } });
    const [track] = await planBoxTracks(jobFor(new Map([[6, [single]]])), [single]);
    assert.equal(track.shapes.length, 1);
    assert.equal(track.shapes[0].frame, 6);
});
test('invalid groups and geometry are rejected before any import', async () => {
    const first = mask(2);
    for (const frames of [
        new Map([[2, [first, mask(2, { clientID: 999 })]]]),
        new Map([[2, [first]], [3, [mask(3, { label: { ...label, id: 2 } })]]]),
        new Map([[2, [first]], [3, [mask(3, { attributes: { 1: 'white', 2: 'standing' } })]]]),
        new Map([[2, [first]], [3, [mask(3, { points: [0] })]]]),
    ]) {
        const job = jobFor(frames);
        await assert.rejects(planBoxTracks(job, [first]));
        assert.equal(job.writes.length, 0);
    }
    assert.throws(() => boxSourceKey(state({ shapeType: 'rectangle' })));
});
test('cancellation leaves annotations unchanged, including cancellation during final read', async () => {
    const abort = new AbortController();
    const job = jobFor(new Map([[2, [state()]]]));
    await assert.rejects(planBoxTracks(job, [state()], () => abort.abort(), abort.signal), /cancelled/);
    assert.equal(job.writes.length, 0);
});


test('conversion keeps originals by default; replacement removes only planned source identities after import', async () => {
    const first = state();
    const m2 = mask(2); const m4 = mask(4);
    const frames = new Map([[2, [first, m2, mask(2, { group: { id: 99 }, clientID: 999 })]],
        [3, [state()]], [4, [state({ outside: true }), m4]]]);
    const keep = jobFor(frames);
    assert.match(await convertToBoxTracks(keep, [first, m2]), /were kept/);
    assert.equal(keep.removals.length, 0);
    const replace = jobFor(frames);
    assert.match(await convertToBoxTracks(replace, [first, m2, m4], true), /replaced/);
    assert.equal(replace.writes[0].tracks.length, 2, 'duplicate mask-group selection is converted once');
    assert.equal(replace.events[0], 'import');
    assert.deepEqual(replace.removals.flat().map((s) => s.clientID), [11, 102, 104]);
});
test('failed import never deletes originals; failed deletion retains boxes and reports partial replacement', async () => {
    const frames = new Map([[2, [mask(2)]], [4, [mask(4)]]]);
    const failedImport = jobFor(frames);
    failedImport.annotations.import = async () => { throw new Error('import failed'); };
    await assert.rejects(convertToBoxTracks(failedImport, [mask(2)], true), /No source annotations were removed/);
    assert.equal(failedImport.removals.length, 0);
    const failedRemoval = jobFor(frames);
    const remove = failedRemoval.annotations.removeBatch;
    failedRemoval.annotations.removeBatch = async (batch) => {
        if (batch[0].frame === 4) throw new Error('delete failed');
        return remove(batch);
    };
    await assert.rejects(convertToBoxTracks(failedRemoval, [mask(2)], true), /stopped after 1\/2.*boxes were kept/);
    assert.equal(failedRemoval.writes.length, 1);
});
test('replacement refuses non-removable reference annotations and detects a skipped removal', async () => {
    const reference = state({ isGroundTruth: true });
    const job = jobFor(new Map([[2, [reference]]]));
    await assert.rejects(convertToBoxTracks(job, [reference], true), /reference annotations cannot be replaced/);
    assert.equal(job.writes.length, 0);
    const skipped = jobFor(new Map([[2, [state()]]]));
    skipped.annotations.removeBatch = async () => [];
    await assert.rejects(convertToBoxTracks(skipped, [state()], true), /did not remove every source/);
    assert.equal(skipped.writes.length, 1);
});
test('cancelling replacement during preparation creates no boxes and deletes no sources', async () => {
    const job = jobFor(new Map([[2, [mask(2)]]]));
    const abort = new AbortController();
    await assert.rejects(convertToBoxTracks(job, [mask(2)], true, () => abort.abort(), abort.signal), /cancelled/);
    assert.equal(job.writes.length, 0);
    assert.equal(job.removals.length, 0);
});
