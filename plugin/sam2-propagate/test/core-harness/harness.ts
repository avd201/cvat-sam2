// Drives the plugin's REAL apply.ts against CVAT's REAL cvat-core Collection (no browser, no server).
// It must live inside a CVAT checkout (cvat-ui/plugins/sam2-propagate/test/core-harness/) because it imports
// cvat-core sources. See run.sh.
import assert from 'node:assert/strict';
import Collection from '../../../../../cvat-core/src/annotations-collection';
import AnnotationHistory from '../../../../../cvat-core/src/annotations-history';
import { Label } from '../../../../../cvat-core/src/labels';
import { applyResults, deleteForward } from '../../src/ts/apply';

const STOP = 20;
const W = 200;
const H = 100;

function makeJob(): { job: any; c: Collection } {
    const label = new Label({
        id: 1, name: 'chicken', color: '#f00', type: 'any', deleted: false,
        attributes: [
            { id: 1, name: 'color', input_type: 'text', mutable: false, values: [''], default_value: '' },
            { id: 2, name: 'pose', input_type: 'text', mutable: true, values: [''], default_value: '' },
        ],
    } as any);
    const framesInfo = new Proxy({ isFrameDeleted: () => false } as any, {
        get: (t, p) => (p in t ? t[p] : { width: W, height: H }),
    });
    const c = new Collection({
        labels: [label], history: new AnnotationHistory(), stopFrame: STOP, dimension: '2d' as any,
        framesInfo, jobType: 'annotation' as any,
    });
    // Mirrors the Job.annotations implementations in cvat-core/src/session-implementation.ts
    const job = {
        stopFrame: STOP,
        annotations: {
            get: async (f: number, all: boolean, filters: object[]) => c.get(f, all, filters),
            import: async (d: any) => { c.import(d); },
            removeBatch: async (s: any, force = false) => c.removeBatch(s, force),
            group: async (s: any, reset: boolean) => c.group(s, reset),
            export: async () => c.export(),
        },
    };
    return { job, c };
}

const maskAt = (x: number) => [0, 100, x, 20, x + 9, 29]; // 10x10 square at (x, 20)
const polyAt = (x: number) => [x, 20, x + 10, 20, x + 10, 30, x, 30];
const stateAt = (c: Collection, frame: number, pick: (s: any) => boolean) => c.get(frame, true, []).find(pick) as any;

async function scenarioMaskRepropagation(): Promise<void> {
    const { job, c } = makeJob();
    c.import({ shapes: [{ type: 'mask', frame: 0, label_id: 1, group: 0, source: 'manual', occluded: false, outside: false, z_order: 0, rotation: 0, points: maskAt(10), attributes: [] }] } as any);
    let src = c.get(0, false, [])[0] as any;
    const res1 = new Map<number, number[] | null>([[0, maskAt(10)], ...[1, 2, 3, 4, 5].map((f) => [f, maskAt(10 + f)] as [number, number[]])]);
    const msg1 = await applyResults({ job, state: src, fromFrame: 0, toFrame: 5, stopFrame: STOP, results: res1, output: 'mask' });
    const gid = (c.get(1, false, [])[0] as any).group.id;
    assert.ok(gid > 0, 'a group id was assigned');
    for (let f = 0; f <= 5; f++) {
        const s = c.get(f, false, []).filter((x: any) => x.shapeType === 'mask');
        assert.equal(s.length, 1, `exactly one mask on frame ${f}`);
        assert.equal((s[0] as any).group.id, gid, `frame ${f} in group ${gid}`);
    }
    assert.equal(c.get(6, false, []).length, 0, 'nothing beyond the propagated range');
    console.log('  mask first run     :', msg1);

    // The operator corrects frame 2, then re-propagates from there through frame 7.
    src = c.get(2, false, [])[0] as any;
    const before1 = JSON.stringify((c.get(1, false, [])[0] as any).points);
    const before2 = JSON.stringify(src.points);
    const res2 = new Map<number, number[] | null>([[2, maskAt(50)], ...[3, 4, 5, 6, 7].map((f) => [f, maskAt(50 + f)] as [number, number[]])]);
    const msg2 = await applyResults({ job, state: src, fromFrame: 2, toFrame: 7, stopFrame: STOP, results: res2, output: 'mask' });
    assert.equal(JSON.stringify((c.get(1, false, [])[0] as any).points), before1, 'frame 1 untouched');
    assert.equal(JSON.stringify((c.get(2, false, [])[0] as any).points), before2, 'frame 2 (the correction) untouched');
    for (let f = 3; f <= 7; f++) {
        const s = c.get(f, false, []).filter((x: any) => x.shapeType === 'mask');
        assert.equal(s.length, 1, `frame ${f}: old mask replaced, not duplicated`);
        assert.equal(s[0].points[2], 50 + f, `frame ${f} follows the corrected object`);
    }
    console.log('  mask re-propagation:', msg2);
}

async function scenarioPolygonTrackFromShape(): Promise<{ job: any; c: Collection }> {
    const { job, c } = makeJob();
    c.import({ shapes: [{ type: 'polygon', frame: 0, label_id: 1, group: 0, source: 'manual', occluded: false, outside: false, z_order: 2, rotation: 0, points: polyAt(10), attributes: [] }] } as any);
    const src = c.get(0, false, [])[0] as any;
    const results = new Map<number, number[] | null>([[0, null], ...[1, 2, 3, 4, 5].map((f) => [f, polyAt(10 + f)] as [number, number[]])]);
    const msg = await applyResults({ job, state: src, fromFrame: 0, toFrame: 5, stopFrame: STOP, results, output: 'polygon' });
    assert.equal(c.get(0, false, []).filter((s: any) => s.objectType === 'shape').length, 0, 'source shape removed');
    const tracks = c.get(3, false, []).filter((s: any) => s.objectType === 'track');
    assert.equal(tracks.length, 1, 'one polygon track');
    for (let f = 0; f <= 5; f++) {
        const s = stateAt(c, f, (x) => x.objectType === 'track');
        assert.equal(s.outside, false, `visible on frame ${f}`);
        assert.equal(s.points[0], 10 + f, `frame ${f} points`);
        assert.equal(s.keyframe, true, `frame ${f} is a keyframe`);
        assert.equal(s.zOrder, 2);
    }
    const closing = stateAt(c, 6, (x) => x.objectType === 'track');
    assert.equal(closing.outside, true, 'closed with an outside keyframe on frame 6');
    assert.equal(closing.keyframe, true);
    assert.equal(c.get(12, false, []).length, 0, 'track no longer shown after its end');
    assert.equal(stateAt(c, 12, (x) => x.objectType === 'track').outside, true);
    console.log('  polygon track new  :', msg);
    return { job, c };
}

async function scenarioTrackInPlace(job: any, c: Collection): Promise<void> {
    const trackBefore = stateAt(c, 3, (x) => x.objectType === 'track');
    const id = trackBefore.clientID;
    const keep2 = JSON.stringify(stateAt(c, 2, (x) => x.clientID === id).points);
    // correct the object on frame 3, then re-propagate 3 -> 9 with the object lost on frames 6-7
    const state = stateAt(c, 3, (x) => x.clientID === id);
    const results = new Map<number, number[] | null>([[3, null], [4, polyAt(60)], [5, polyAt(61)], [6, null], [7, null], [8, polyAt(64)], [9, polyAt(65)]]);
    const msg = await applyResults({ job, state, fromFrame: 3, toFrame: 9, stopFrame: STOP, results, output: 'polygon' });
    assert.equal(stateAt(c, 3, (x) => x.clientID === id).points[0], 13, 'prompt frame untouched');
    assert.equal(JSON.stringify(stateAt(c, 2, (x) => x.clientID === id).points), keep2, 'earlier frames untouched');
    assert.equal(stateAt(c, 4, (x) => x.clientID === id).points[0], 60, 'frame 4 re-propagated');
    assert.equal(stateAt(c, 6, (x) => x.clientID === id).outside, true, 'object lost on 6');
    assert.equal(stateAt(c, 7, (x) => x.clientID === id).outside, true, 'object lost on 7');
    assert.equal(stateAt(c, 8, (x) => x.clientID === id).outside, false, 'found again on 8');
    assert.equal(stateAt(c, 8, (x) => x.clientID === id).points[0], 64);
    assert.equal(stateAt(c, 9, (x) => x.clientID === id).points[0], 65);
    assert.equal(stateAt(c, 10, (x) => x.clientID === id).outside, true, 'new closing keyframe on 10');
    assert.equal(stateAt(c, 15, (x) => x.clientID === id).outside, true, 'still ended further on');
    assert.equal(c.get(0, true, []).filter((s: any) => s.objectType === 'track').length, 1, 'still ONE track: identity preserved');
    console.log('  polygon in place   :', msg);

    // extend the same track further: the previous closing marker must be replaced, not left in the middle
    const s9 = stateAt(c, 9, (x) => x.clientID === id);
    const res3 = new Map<number, number[] | null>([[9, null], [10, polyAt(70)], [11, polyAt(71)]]);
    await applyResults({ job, state: s9, fromFrame: 9, toFrame: 11, stopFrame: STOP, results: res3, output: 'polygon' });
    assert.equal(stateAt(c, 10, (x) => x.clientID === id).outside, false, 'old marker on 10 replaced by a real keyframe');
    assert.equal(stateAt(c, 11, (x) => x.clientID === id).points[0], 71);
    assert.equal(stateAt(c, 12, (x) => x.clientID === id).outside, true, 'new marker on 12');
}

async function scenarioMaskToPolygonTrack(): Promise<void> {
    const { job, c } = makeJob();
    c.import({ shapes: [{ type: 'mask', frame: 0, label_id: 1, group: 0, source: 'manual', occluded: false, outside: false, z_order: 0, rotation: 0, points: maskAt(10), attributes: [] }] } as any);
    const src = c.get(0, false, [])[0] as any;
    const results = new Map<number, number[] | null>([[0, polyAt(10)], [1, polyAt(11)], [2, polyAt(12)]]);
    const msg = await applyResults({ job, state: src, fromFrame: 0, toFrame: 2, stopFrame: STOP, results, output: 'polygon' });
    assert.equal(stateAt(c, 0, (x) => x.objectType === 'track').points[0], 10, "first keyframe = polygon of the user's mask");
    assert.equal(c.get(0, false, []).filter((s: any) => s.objectType === 'shape').length, 0, 'mask shape replaced');
    console.log('  mask -> poly track :', msg);
}

async function scenarioEndOfJob(): Promise<void> {
    const { job, c } = makeJob();
    c.import({ shapes: [{ type: 'polygon', frame: 18, label_id: 1, group: 0, source: 'manual', occluded: false, outside: false, z_order: 0, rotation: 0, points: polyAt(10), attributes: [] }] } as any);
    const src = c.get(18, false, [])[0] as any;
    await applyResults({ job, state: src, fromFrame: 18, toFrame: STOP, stopFrame: STOP, output: 'polygon',
        results: new Map<number, number[] | null>([[18, null], [19, polyAt(11)], [20, polyAt(12)]]) });
    assert.equal(stateAt(c, 20, (x) => x.objectType === 'track').outside, false, 'no marker needed at the end of the job');
}


// ---- forward delete ----------------------------------------------------------------------------------------------
async function denseTrack(): Promise<{ job: any; c: Collection; id: number }> {
    const { job, c } = makeJob();
    c.import({ shapes: [{ type: 'polygon', frame: 0, label_id: 1, group: 0, source: 'manual', occluded: false, outside: false, z_order: 0, rotation: 0, points: polyAt(10), attributes: [] }] } as any);
    const src = c.get(0, false, [])[0] as any;
    const results = new Map<number, number[] | null>([[0, null], ...[1, 2, 3, 4, 5, 6, 7, 8, 9, 10].map((f) => [f, polyAt(10 + f)] as [number, number[]])]);
    await applyResults({ job, state: src, fromFrame: 0, toFrame: 10, stopFrame: STOP, results, output: 'polygon' });
    const id = stateAt(c, 0, (x) => x.objectType === 'track').clientID;
    return { job, c, id };
}

async function scenarioDeleteForwardTrack(): Promise<void> {
    // (a) the whole point: one keyframe per frame, delete from frame 5 on (including 5) -> 0-4 intact, 5.. gone
    let { job, c, id } = await denseTrack();
    const before = [0, 1, 2, 3, 4].map((f) => JSON.stringify(stateAt(c, f, (x) => x.clientID === id).points));
    let msg = await deleteForward({ job, state: stateAt(c, 5, (x) => x.clientID === id), frame: 5, stopFrame: STOP, includeCurrent: true });
    for (let f = 0; f <= 4; f++) {
        const st = stateAt(c, f, (x) => x.clientID === id);
        assert.equal(st.outside, false, `frame ${f} still visible`);
        assert.equal(JSON.stringify(st.points), before[f], `frame ${f} unchanged`);
    }
    for (const f of [5, 6, 8, 11, 15, 20]) assert.equal(stateAt(c, f, (x) => x.clientID === id).outside, true, `frame ${f} gone`);
    assert.equal(c.get(7, false, []).filter((x: any) => x.objectType === 'track').length, 0, 'nothing drawn on frame 7');
    assert.equal(c.get(0, true, []).filter((x: any) => x.objectType === 'track').length, 1, 'still the same single track');
    console.log('  delete track fwd   :', msg);

    // (b) keep the current frame: delete only the following frames
    ({ job, c, id } = await denseTrack());
    msg = await deleteForward({ job, state: stateAt(c, 5, (x) => x.clientID === id), frame: 5, stopFrame: STOP, includeCurrent: false });
    assert.equal(stateAt(c, 5, (x) => x.clientID === id).outside, false, 'frame 5 kept');
    assert.equal(stateAt(c, 5, (x) => x.clientID === id).points[0], 15);
    assert.equal(stateAt(c, 6, (x) => x.clientID === id).outside, true, 'frame 6 gone');
    assert.equal(stateAt(c, 10, (x) => x.clientID === id).outside, true, 'frame 10 gone');

    // (c) cutting at (or before) the first keyframe removes the whole object
    ({ job, c, id } = await denseTrack());
    msg = await deleteForward({ job, state: stateAt(c, 0, (x) => x.clientID === id), frame: 0, stopFrame: STOP, includeCurrent: true });
    assert.equal(c.get(3, true, []).filter((x: any) => x.objectType === 'track').length, 0, 'whole track removed');
    console.log('  delete from start  :', msg);

    // (d) a sparse (interpolated) track: cutting between keyframes ends it there
    const sparse = makeJob();
    sparse.c.import({ tracks: [{ frame: 0, label_id: 1, group: 0, source: 'manual', attributes: [], shapes: [
        { type: 'polygon', frame: 0, points: polyAt(0), outside: false, occluded: false, z_order: 0, rotation: 0, attributes: [] },
        { type: 'polygon', frame: 10, points: polyAt(10), outside: false, occluded: false, z_order: 0, rotation: 0, attributes: [] },
    ] }] } as any);
    const sid = stateAt(sparse.c, 0, (x) => x.objectType === 'track').clientID;
    await deleteForward({ job: sparse.job, state: stateAt(sparse.c, 4, (x) => x.clientID === sid), frame: 4, stopFrame: STOP, includeCurrent: true });
    assert.equal(stateAt(sparse.c, 3, (x) => x.clientID === sid).outside, false, 'frame 3 visible');
    assert.equal(stateAt(sparse.c, 4, (x) => x.clientID === sid).outside, true, 'ended on 4');
    assert.equal(stateAt(sparse.c, 10, (x) => x.clientID === sid).outside, true, 'the keyframe on 10 is gone too');

    // (e) a propagation after a forward delete still works (the track can be grown again)
    ({ job, c, id } = await denseTrack());
    await deleteForward({ job, state: stateAt(c, 5, (x) => x.clientID === id), frame: 5, stopFrame: STOP, includeCurrent: true });
    const s4 = stateAt(c, 4, (x) => x.clientID === id);
    await applyResults({ job, state: s4, fromFrame: 4, toFrame: 7, stopFrame: STOP, output: 'polygon',
        results: new Map<number, number[] | null>([[4, null], [5, polyAt(50)], [6, polyAt(51)], [7, polyAt(52)]]) });
    assert.equal(stateAt(c, 7, (x) => x.clientID === id).points[0], 52);
    assert.equal(stateAt(c, 8, (x) => x.clientID === id).outside, true);
}

async function scenarioDeleteForwardMasks(): Promise<void> {
    const { job, c } = makeJob();
    c.import({ shapes: [{ type: 'mask', frame: 0, label_id: 1, group: 0, source: 'manual', occluded: false, outside: false, z_order: 0, rotation: 0, points: maskAt(10), attributes: [] }] } as any);
    const src = c.get(0, false, [])[0] as any;
    const res = new Map<number, number[] | null>([[0, maskAt(10)], ...[1, 2, 3, 4, 5, 6].map((f) => [f, maskAt(10 + f)] as [number, number[]])]);
    await applyResults({ job, state: src, fromFrame: 0, toFrame: 6, stopFrame: STOP, results: res, output: 'mask' });
    // a second, independent object must not be touched
    c.import({ shapes: [{ type: 'mask', frame: 5, label_id: 1, group: 0, source: 'manual', occluded: false, outside: false, z_order: 0, rotation: 0, points: maskAt(100), attributes: [] }] } as any);
    const msg = await deleteForward({ job, state: c.get(3, false, []).filter((x: any) => x.shapeType === 'mask')[0] as any, frame: 3, stopFrame: STOP, includeCurrent: true });
    for (let f = 0; f <= 2; f++) assert.equal(c.get(f, false, []).filter((x: any) => x.shapeType === 'mask').length, 1, `frame ${f} intact`);
    assert.equal(c.get(3, false, []).filter((x: any) => x.shapeType === 'mask').length, 0, 'frame 3 deleted');
    assert.equal(c.get(4, false, []).filter((x: any) => x.shapeType === 'mask').length, 0, 'frame 4 deleted');
    const f5 = c.get(5, false, []).filter((x: any) => x.shapeType === 'mask');
    assert.equal(f5.length, 1, 'frame 5: only the OTHER object remains');
    assert.equal((f5[0] as any).points[2], 100, 'the other object is untouched');
    console.log('  delete masks fwd   :', msg);
}

// ---- redrawn mask joins the existing object --------------------------------------------------------------------------
async function scenarioRedrawnMaskJoinsObject(): Promise<void> {
    const { job, c } = makeJob();
    c.import({ shapes: [{ type: 'mask', frame: 0, label_id: 1, group: 0, source: 'manual', occluded: false, outside: false, z_order: 0, rotation: 0, points: maskAt(10), attributes: [{ spec_id: 1, value: 'x' }] }] } as any);
    let src = c.get(0, false, [])[0] as any;
    await applyResults({ job, state: src, fromFrame: 0, toFrame: 6, stopFrame: STOP, output: 'mask',
        results: new Map<number, number[] | null>([[0, maskAt(10)], ...[1, 2, 3, 4, 5, 6].map((f) => [f, maskAt(10 + f)] as [number, number[]])]) });
    const gid = (c.get(1, false, [])[0] as any).group.id;

    // WITHOUT choosing the object: the old behaviour -> old and new masks pile up (this is what you saw)
    c.import({ shapes: [{ type: 'mask', frame: 3, label_id: 1, group: 0, source: 'manual', occluded: false, outside: false, z_order: 0, rotation: 0, points: maskAt(60), attributes: [{ spec_id: 1, value: 'x' }, { spec_id: 2, value: 'y' }] }] } as any);
    const redrawn = c.get(3, false, []).filter((x: any) => !x.group || x.group.id !== gid)[0] as any;
    assert.ok(redrawn && redrawn.points[2] === 60, 'found the redrawn mask');

    // WITH the object chosen: the redrawn mask joins group `gid`; old masks 3..7 are replaced; 0-2 untouched
    const keep2 = JSON.stringify(c.get(2, false, [])[0] as any && (c.get(2, false, [])[0] as any).points);
    const msg = await applyResults({ job, state: redrawn, fromFrame: 3, toFrame: 7, stopFrame: STOP, output: 'mask', targetGroup: gid,
        results: new Map<number, number[] | null>([[3, maskAt(60)], ...[4, 5, 6, 7].map((f) => [f, maskAt(60 + f)] as [number, number[]])]) });
    for (let f = 0; f <= 7; f++) {
        const masks = c.get(f, false, []).filter((x: any) => x.shapeType === 'mask');
        assert.equal(masks.length, 1, `frame ${f}: exactly one mask (no old+new pile-up)`);
        assert.equal((masks[0] as any).group.id, gid, `frame ${f} belongs to the same object`);
    }
    assert.equal(JSON.stringify((c.get(2, false, [])[0] as any).points), keep2, 'frames before the correction untouched');
    assert.equal((c.get(3, false, [])[0] as any).points[2], 60, 'frame 3 is the corrected mask');
    assert.equal((c.get(7, false, [])[0] as any).points[2], 67, 'propagated from the correction');
    assert.deepEqual((c.get(4, false, [])[0] as any).attributes, { 1: 'x', 2: 'y' }, "the object takes the attributes of the mask you propagated from");
    console.log('  redrawn joins obj  :', msg);
}

async function scenarioAttributesCarried(): Promise<void> {
    // polygon SHAPE -> polygon track: immutable attr must land on the track, mutable on every keyframe
    const { job, c } = makeJob();
    c.import({ shapes: [{ type: 'polygon', frame: 0, label_id: 1, group: 0, source: 'manual', occluded: false, outside: false, z_order: 0, rotation: 0, points: polyAt(10), attributes: [{ spec_id: 1, value: 'red' }, { spec_id: 2, value: 'up' }] }] } as any);
    const state = c.get(0, false, [])[0] as any;
    assert.deepEqual(state.attributes, { 1: 'red', 2: 'up' }, 'source attributes as core reports them');
    await applyResults({ job, state, fromFrame: 0, toFrame: 3, stopFrame: STOP, output: 'polygon',
        results: new Map<number, number[] | null>([[0, null], [1, polyAt(11)], [2, polyAt(12)], [3, polyAt(13)]]) });
    for (let f = 0; f <= 3; f++) {
        assert.deepEqual(stateAt(c, f, (x) => x.objectType === 'track').attributes, { 1: 'red', 2: 'up' }, `track attributes on frame ${f}`);
    }

    // mask SHAPE -> mask shapes: all attributes on each shape
    const m = makeJob();
    m.c.import({ shapes: [{ type: 'mask', frame: 0, label_id: 1, group: 0, source: 'manual', occluded: false, outside: false, z_order: 0, rotation: 0, points: maskAt(10), attributes: [{ spec_id: 1, value: 'blue' }, { spec_id: 2, value: 'down' }] }] } as any);
    await applyResults({ job: m.job, state: m.c.get(0, false, [])[0] as any, fromFrame: 0, toFrame: 2, stopFrame: STOP, output: 'mask',
        results: new Map<number, number[] | null>([[0, maskAt(10)], [1, maskAt(11)], [2, maskAt(12)]]) });
    for (const f of [1, 2]) {
        assert.deepEqual((m.c.get(f, false, [])[0] as any).attributes, { 1: 'blue', 2: 'down' }, `mask attributes on frame ${f}`);
    }
    console.log('  attributes carried : ok (polygon track + mask shapes)');
}

// ---- several objects at once (the plugin applies them one after another, from states captured up front) ---------------
async function scenarioTwoMaskObjects(): Promise<void> {
    const { job, c } = makeJob();
    const imp = (x: number) => c.import({ shapes: [{ type: 'mask', frame: 0, label_id: 1, group: 0, source: 'manual', occluded: false, outside: false, z_order: 0, rotation: 0, points: maskAt(x), attributes: [] }] } as any);
    imp(10); imp(100);
    const states = c.get(0, false, []).filter((s: any) => s.shapeType === 'mask') as any[];
    assert.equal(states.length, 2);
    const [a, b] = states.sort((p, q) => p.points[2] - q.points[2]);      // a at x=10, b at x=100 (captured BEFORE any write)
    const res = (x0: number, n = 5) => new Map<number, number[] | null>([[0, maskAt(x0)], ...Array.from({ length: n }, (_, i) => [i + 1, maskAt(x0 + i + 1)] as [number, number[]])]);
    await applyResults({ job, state: a, fromFrame: 0, toFrame: 5, stopFrame: STOP, results: res(10), output: 'mask', targetGroup: null });
    await applyResults({ job, state: b, fromFrame: 0, toFrame: 5, stopFrame: STOP, results: res(100), output: 'mask', targetGroup: null });

    const masks = (f: number) => c.get(f, false, []).filter((s: any) => s.shapeType === 'mask') as any[];
    const ga = masks(1).find((s) => s.points[2] === 11).group.id;
    const gb = masks(1).find((s) => s.points[2] === 101).group.id;
    assert.ok(ga > 0 && gb > 0 && ga !== gb, 'each object got its own group');
    for (let f = 0; f <= 5; f++) {
        const m = masks(f);
        assert.equal(m.length, 2, `frame ${f}: both objects present`);
        assert.equal(m.find((s) => s.group.id === ga).points[2], 10 + f, `object A follows A on frame ${f}`);
        assert.equal(m.find((s) => s.group.id === gb).points[2], 100 + f, `object B follows B on frame ${f}`);
    }

    // re-propagate ONLY object A from frame 3: B must be byte-for-byte untouched
    const bBefore = JSON.stringify([0, 1, 2, 3, 4, 5].map((f) => masks(f).find((s) => s.group.id === gb).points));
    const aAt3 = masks(3).find((s) => s.group.id === ga);
    await applyResults({ job, state: aAt3, fromFrame: 3, toFrame: 7, stopFrame: STOP, output: 'mask', targetGroup: null,
        results: new Map<number, number[] | null>([[3, maskAt(40)], ...[4, 5, 6, 7].map((f) => [f, maskAt(40 + f)] as [number, number[]])]) });
    assert.equal(JSON.stringify([0, 1, 2, 3, 4, 5].map((f) => masks(f).find((s) => s.group.id === gb).points)), bBefore, 'object B untouched');
    assert.equal(masks(6).length, 1, 'only A continues to frame 6');
    assert.equal(masks(5).filter((s) => s.group.id === ga).length, 1, 'A replaced, not duplicated');

    // multi-object delete from frame 4 (both selected)
    const sel = [masks(4).find((s) => s.group.id === ga), masks(4).find((s) => s.group.id === gb)];
    for (const st of sel) await deleteForward({ job, state: st, frame: 4, stopFrame: STOP, includeCurrent: true });
    for (let f = 0; f <= 3; f++) assert.equal(masks(f).length, 2, `frame ${f} intact`);
    for (let f = 4; f <= 8; f++) assert.equal(masks(f).length, 0, `frame ${f} cleared for both`);
    console.log('  two mask objects   : independent propagate / re-propagate / delete-forward ok');
}

async function scenarioMixedTrackAndShape(): Promise<void> {
    const { job, c } = makeJob();
    // an existing dense polygon track (object T) ...
    c.import({ shapes: [{ type: 'polygon', frame: 0, label_id: 1, group: 0, source: 'manual', occluded: false, outside: false, z_order: 0, rotation: 0, points: polyAt(10), attributes: [] }] } as any);
    await applyResults({ job, state: c.get(0, false, [])[0] as any, fromFrame: 0, toFrame: 4, stopFrame: STOP, output: 'polygon',
        results: new Map<number, number[] | null>([[0, null], ...[1, 2, 3, 4].map((f) => [f, polyAt(10 + f)] as [number, number[]])]) });
    const tid = stateAt(c, 0, (x) => x.objectType === 'track').clientID;
    // ... plus a fresh mask shape (object S) on frame 4; both selected on frame 4, one polygon-track output for both
    c.import({ shapes: [{ type: 'mask', frame: 4, label_id: 1, group: 0, source: 'manual', occluded: false, outside: false, z_order: 0, rotation: 0, points: maskAt(100), attributes: [] }] } as any);
    const track = stateAt(c, 4, (x) => x.clientID === tid);
    const shape = c.get(4, false, []).find((s: any) => s.objectType === 'shape') as any;
    const results = (x0: number) => new Map<number, number[] | null>([[4, polyAt(x0)], ...[5, 6, 7].map((f) => [f, polyAt(x0 + f - 4)] as [number, number[]])]);
    await applyResults({ job, state: track, fromFrame: 4, toFrame: 7, stopFrame: STOP, output: 'polygon', results: results(60) });
    await applyResults({ job, state: shape, fromFrame: 4, toFrame: 7, stopFrame: STOP, output: 'polygon', results: results(120) });
    const tracks = c.get(6, false, []).filter((s: any) => s.objectType === 'track') as any[];
    assert.equal(tracks.length, 2, 'two tracks on frame 6: the existing one and the new one');
    assert.ok(tracks.some((t) => t.clientID === tid && t.points[0] === 62), 'the existing track was updated in place');
    assert.ok(tracks.some((t) => t.clientID !== tid && t.points[0] === 122), 'the mask became a new polygon track');
    assert.equal(c.get(4, false, []).filter((s: any) => s.objectType === 'shape').length, 0, 'the loose mask shape is gone');
    assert.equal(stateAt(c, 3, (x) => x.clientID === tid).points[0], 13, "the existing track's earlier frames are untouched");
    for (const t of tracks) assert.equal(stateAt(c, 8, (x) => x.clientID === t.clientID).outside, true, 'both are closed after frame 7');
    // delete both tracks from frame 6
    for (const t of tracks) await deleteForward({ job, state: stateAt(c, 6, (x) => x.clientID === t.clientID), frame: 6, stopFrame: STOP, includeCurrent: true });
    assert.equal(c.get(5, false, []).filter((s: any) => s.objectType === 'track').length, 2, 'frame 5 still has both');
    // on the cut frame the track keeps an `outside` KEYFRAME (listed, but nothing is drawn); after it, nothing at all
    for (const t of tracks) assert.equal(stateAt(c, 6, (x) => x.clientID === t.clientID).outside, true, 'frame 6: both ended');
    assert.equal(c.get(7, false, []).filter((s: any) => s.objectType === 'track').length, 0, 'frame 7 has none');
    console.log('  track + mask mixed : in-place update, new polygon track, multi delete-forward ok');
}

(async () => {
    console.log('cvat-core harness:');
    await scenarioMaskRepropagation();
    const { job, c } = await scenarioPolygonTrackFromShape();
    await scenarioTrackInPlace(job, c);
    await scenarioMaskToPolygonTrack();
    await scenarioEndOfJob();
    await scenarioDeleteForwardTrack();
    await scenarioDeleteForwardMasks();
    await scenarioRedrawnMaskJoinsObject();
    await scenarioAttributesCarried();
    await scenarioTwoMaskObjects();
    await scenarioMixedTrackAndShape();
    console.log('ALL SCENARIOS PASSED');
})().catch((e) => { console.error('HARNESS FAILURE:', e); process.exit(1); });
