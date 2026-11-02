// Run:  node --experimental-strip-types --test test/        (Node >= 22.6)
import test from 'node:test';
import assert from 'node:assert/strict';
import {
    NdjsonParser, allowedOutputs, unsupportedReason, pointsAreValid, buildBody, toAbsolute, errorText,
    closingMarkerFrame, planTrackUpdates, buildPolygonTrack, buildMaskShapes,
    serializeAttributes, splitAttributes, summarizeGroups, suggestGroup, deleteCutFrame, buildMultiBody, commonOutputs,
} from '../src/ts/plan.ts';

const poly = (x) => [x, 0, x + 10, 0, x + 10, 10, x, 10];

test('NDJSON parser reassembles lines split across chunks and flushes a final unterminated line', () => {
    const p = new NdjsonParser();
    assert.deepEqual(p.feed('{"frame":0,"po'), []);
    assert.deepEqual(p.feed('ints":[1,2]}\n{"frame":1,"points":null}\n{"do'), [
        { frame: 0, points: [1, 2] }, { frame: 1, points: null },
    ]);
    assert.deepEqual(p.feed('ne":true,"frames":2}'), []);
    assert.deepEqual(p.flush(), [{ done: true, frames: 2 }]);
    assert.deepEqual(p.flush(), []);
});

test('allowed outputs per selected object', () => {
    assert.deepEqual(allowedOutputs('shape', 'mask'), ['mask', 'polygon']);
    assert.deepEqual(allowedOutputs('shape', 'polygon'), ['polygon', 'mask']);
    assert.deepEqual(allowedOutputs('track', 'polygon'), ['polygon']);
    assert.deepEqual(allowedOutputs('track', 'mask'), []);
    assert.deepEqual(allowedOutputs('shape', 'rectangle'), []);
    assert.match(unsupportedReason('track', 'mask'), /Mask tracks cannot be used/);
    assert.match(unsupportedReason('shape', 'rectangle'), /rectangle/);
});

test('points validity mirrors the server rules', () => {
    assert.equal(pointsAreValid('polygon', [1, 2, 3, 4, 5, 6]), true);
    assert.equal(pointsAreValid('polygon', [1, 2, 3, 4, 5]), false);   // odd
    assert.equal(pointsAreValid('polygon', [1, 2, 3, 4]), false);      // too short
    assert.equal(pointsAreValid('mask', [1, 2, 3, 0, 0, 5, 5]), true);
    assert.equal(pointsAreValid('mask', [1, 2, 3, 4]), false);
    assert.equal(pointsAreValid('polygon', null), true);               // "not found" is fine
});

test('request body uses the snake_case contract of /v1/video/propagate-job', () => {
    const b = buildBody({ jobId: 7, org: 'o', fromFrame: 3, toFrame: 9, promptType: 'mask', promptPoints: [1], output: 'polygon', simplifyPx: 1.5 });
    assert.deepEqual(b, { job_id: 7, org: 'o', from_frame: 3, to_frame: 9, prompt_type: 'mask', prompt_points: [1], output: 'polygon', simplify_px: 1.5 });
});

test('relative frame numbers become absolute job frames', () => {
    const abs = toAbsolute(new Map([[0, [1]], [2, null]]), 30);
    assert.deepEqual([...abs.entries()], [[30, [1]], [32, null]]);
});

test('error texts are specific', () => {
    assert.match(errorText(401, '{"detail":"CVAT refused the request"}'), /Not allowed \(HTTP 401\). CVAT refused/);
    assert.match(errorText(404, '404 page not found'), /route is not active/);
    assert.match(errorText(405, '<html><head><title>405 Not Allowed</title></head></html>'), /route is not active in CVAT's Traefik \(HTTP 405/);
    assert.match(errorText(405, '{"detail":"Method Not Allowed"}'), /HTTP 405: Method Not Allowed/); // FastAPI's own 405 is JSON
    assert.match(errorText(404, '{"detail":"CVAT: job or frame not found"}'), /HTTP 404: CVAT: job or frame not found/);
    assert.match(errorText(429, '{"detail":"busy"}'), /busy/);
    assert.match(errorText(502, 'Bad Gateway'), /not reachable/);
    assert.match(errorText(502, '{"detail":"CVAT answered HTTP 400 for /api/users/self"}'), /SAM 2 service error \(HTTP 502\): CVAT answered HTTP 400/);
    assert.match(errorText(400, '{"detail":[{"msg":"x"}]}'), /HTTP 400: \[\{"msg":"x"\}\]/);
});

test('closing marker: needed only when the track would otherwise continue', () => {
    const base = { toFrame: 10, stopFrame: 178, lastLost: false, nextHasKeyframe: false };
    assert.equal(closingMarkerFrame(base), 11);
    assert.equal(closingMarkerFrame({ ...base, stopFrame: 10 }), null);        // end of job
    assert.equal(closingMarkerFrame({ ...base, lastLost: true }), null);       // range already ends outside
    assert.equal(closingMarkerFrame({ ...base, nextHasKeyframe: true }), null); // a keyframe follows immediately
});

test('track update plan covers (from, to] and refuses gaps', () => {
    const r = new Map([[11, poly(1)], [12, null], [13, poly(3)]]);
    assert.deepEqual(planTrackUpdates(r, 10, 13).map((s) => [s.frame, s.points === null]), [[11, false], [12, true], [13, false]]);
    assert.throws(() => planTrackUpdates(new Map([[11, poly(1)]]), 10, 12), /no result for frame 12/);
});

test('new polygon track: own first keyframe, outside keyframes repeat the last polygon, range is closed', () => {
    const results = new Map([[11, poly(1)], [12, null], [13, null], [14, poly(4)]]);
    const t = buildPolygonTrack({ fromFrame: 10, toFrame: 14, stopFrame: 178, firstPoints: poly(0), results, labelId: 5, group: 2, source: 'semi-auto', zOrder: 3 });
    assert.deepEqual(t.shapes.map((s) => [s.frame, s.outside]), [[10, false], [11, false], [12, true], [13, true], [14, false], [15, true]]);
    assert.deepEqual(t.shapes[2].points, poly(1));                 // lost frames repeat the last known polygon
    assert.deepEqual(t.shapes[5].points, poly(4));                 // closing marker too
    assert.deepEqual(t.shapes[0].points, poly(0));
    assert.equal(t.frame, 10); assert.equal(t.label_id, 5); assert.equal(t.group, 2); assert.equal(t.shapes[0].z_order, 3);
    assert.ok(t.shapes.every((s) => s.type === 'polygon' && s.points.length === 8 && s.rotation === 0 && s.occluded === false));
});

test('new polygon track ending with a lost object, or at the end of the job, gets no extra marker', () => {
    const lost = buildPolygonTrack({ fromFrame: 0, toFrame: 2, stopFrame: 178, firstPoints: poly(0), results: new Map([[1, poly(1)], [2, null]]), labelId: 1, group: 0, source: 's', zOrder: 0 });
    assert.deepEqual(lost.shapes.map((s) => s.frame), [0, 1, 2]);
    const atEnd = buildPolygonTrack({ fromFrame: 0, toFrame: 2, stopFrame: 2, firstPoints: poly(0), results: new Map([[1, poly(1)], [2, poly(2)]]), labelId: 1, group: 0, source: 's', zOrder: 0 });
    assert.deepEqual(atEnd.shapes.map((s) => s.frame), [0, 1, 2]);
});

test('mask shapes: one per found frame, same group, none for lost frames', () => {
    const m = [3, 4, 2, 0, 0, 1, 1];
    const shapes = buildMaskShapes({ fromFrame: 5, toFrame: 8, results: new Map([[6, m], [7, null], [8, m]]), labelId: 4, group: 9, source: 'semi-auto', zOrder: 1 });
    assert.deepEqual(shapes.map((s) => [s.frame, s.group, s.type, s.label_id]), [[6, 9, 'mask', 4], [8, 9, 'mask', 4]]);
    assert.deepEqual(shapes[0].points, m);
    assert.throws(() => buildMaskShapes({ fromFrame: 5, toFrame: 7, results: new Map([[6, m]]), labelId: 1, group: 1, source: 's', zOrder: 0 }), /no result for frame 7/);
});

test('attributes are carried over in serialized form', () => {
    assert.deepEqual(serializeAttributes({ 3: 'red', 7: '2' }), [{ spec_id: 3, value: 'red' }, { spec_id: 7, value: '2' }]);
    assert.deepEqual(serializeAttributes(undefined), []);
    const attrs = [{ spec_id: 1, value: 'a' }];
    const t = buildPolygonTrack({ fromFrame: 0, toFrame: 1, stopFrame: 9, firstPoints: poly(0), results: new Map([[1, poly(1)]]), labelId: 1, group: 0, source: 's', zOrder: 0, attributes: attrs });
    assert.ok(t.shapes.every((s) => JSON.stringify(s.attributes) === JSON.stringify(attrs)));
    assert.notEqual(t.shapes[0].attributes, t.shapes[1].attributes);   // copies, not one shared array
    const m = buildMaskShapes({ fromFrame: 0, toFrame: 1, results: new Map([[1, [0, 4, 1, 1, 2, 2]]]), labelId: 1, group: 1, source: 's', zOrder: 0, attributes: attrs });
    assert.deepEqual(m[0].attributes, attrs);
});

test('group summaries list mask objects only', () => {
    const shapes = [
        { type: 'mask', group: 2, frame: 5, label_id: 1 }, { type: 'mask', group: 2, frame: 9, label_id: 1 },
        { type: 'mask', group: 0, frame: 1, label_id: 1 }, { type: 'polygon', group: 3, frame: 1, label_id: 1 },
        { type: 'mask', group: 4, frame: 2, label_id: 2 },
    ];
    assert.deepEqual(summarizeGroups(shapes), [
        { id: 2, labelId: 1, first: 5, last: 9, count: 2 }, { id: 4, labelId: 2, first: 2, last: 2, count: 1 },
    ]);
});

test('suggested object for a redrawn mask: exactly one neighbour group with the same label, else none', () => {
    const shapes = [{ type: 'mask', group: 2, frame: 4, label_id: 1 }, { type: 'mask', group: 3, frame: 4, label_id: 2 }];
    assert.equal(suggestGroup(shapes, 5, 1, 0), 2);                  // frame 4 is adjacent, label matches
    assert.equal(suggestGroup(shapes, 5, 2, 0), 3);
    assert.equal(suggestGroup(shapes, 9, 1, 0), null);               // nothing near
    assert.equal(suggestGroup(shapes, 5, 1, 2), null);               // own group is excluded
    assert.equal(suggestGroup([...shapes, { type: 'mask', group: 7, frame: 6, label_id: 1 }], 5, 1, 0), null); // ambiguous
});

test('delete cut frame', () => {
    assert.equal(deleteCutFrame(10, true), 10);
    assert.equal(deleteCutFrame(10, false), 11);
});

test('attributes are split: immutable on the track, mutable on each keyframe', () => {
    const specs = [{ id: 1, mutable: false }, { id: 2, mutable: true }];
    assert.deepEqual(splitAttributes({ 1: 'red', 2: 'up' }, specs), {
        immutable: [{ spec_id: 1, value: 'red' }], mutable: [{ spec_id: 2, value: 'up' }],
    });
    const t = buildPolygonTrack({ fromFrame: 0, toFrame: 1, stopFrame: 9, firstPoints: poly(0), results: new Map([[1, poly(1)]]), labelId: 1, group: 0, source: 's', zOrder: 0, attributes: [{ spec_id: 2, value: 'up' }], trackAttributes: [{ spec_id: 1, value: 'red' }] });
    assert.deepEqual(t.attributes, [{ spec_id: 1, value: 'red' }]);
    assert.ok(t.shapes.every((s) => s.attributes.length === 1 && s.attributes[0].spec_id === 2));
});

test('multi-object request: one body, objects keyed by a caller id, no single-prompt fields', () => {
    const b = buildMultiBody({
        jobId: 7, org: 'o', fromFrame: 3, toFrame: 9, output: 'mask', simplifyPx: 1.5,
        objects: [{ id: 1, promptType: 'mask', promptPoints: [1, 2] }, { id: 2, promptType: 'polygon', promptPoints: [3, 4, 5, 6, 7, 8] }],
    });
    assert.deepEqual(b.objects, [
        { id: 1, prompt_type: 'mask', prompt_points: [1, 2] }, { id: 2, prompt_type: 'polygon', prompt_points: [3, 4, 5, 6, 7, 8] },
    ]);
    assert.equal(b.prompt_type, undefined);
    assert.equal(b.prompt_points, undefined);
    assert.equal(JSON.parse(JSON.stringify(b)).from_frame, 3);
});

test('one output format for a mixed selection', () => {
    const maskShape = allowedOutputs('shape', 'mask');       // [mask, polygon]
    const polyShape = allowedOutputs('shape', 'polygon');    // [polygon, mask]
    const polyTrack = allowedOutputs('track', 'polygon');    // [polygon]
    assert.deepEqual(commonOutputs([maskShape, maskShape]), ['mask', 'polygon']);
    assert.deepEqual(commonOutputs([maskShape, polyShape]), ['mask', 'polygon']);
    assert.deepEqual(commonOutputs([maskShape, polyTrack]), ['polygon']);   // a track forces polygon for everyone
    assert.deepEqual(commonOutputs([polyTrack, polyTrack]), ['polygon']);
    assert.deepEqual(commonOutputs([maskShape, []]), []);                   // an unsupported object blocks the run
    assert.deepEqual(commonOutputs([]), []);
});

test('a 422 hints at an outdated service', () => {
    assert.match(errorText(422, '{"detail":[{"msg":"Field required"}]}'), /may be outdated; rebuild sam2-service/);
});
