// Copyright (C) 2026
//
// SPDX-License-Identifier: MIT

// Writes propagation results into CVAT's IN-MEMORY annotations through the public core API
// (job.annotations.*), exactly like the UI does; the user then presses Save (Ctrl+S).

import { Job, ObjectState } from 'cvat-core-wrapper';
import {
    OutputKind, buildMaskShapes, buildPolygonTrack, closingMarkerFrame, deleteCutFrame, planTrackUpdates,
    serializeAttributes, splitAttributes,
} from './plan';

export interface ApplyContext {
    job: Job;
    state: ObjectState; // the selected object, as shown on the current frame
    fromFrame: number;
    toFrame: number;
    stopFrame: number;
    results: Map<number, number[] | null>; // absolute frames, including fromFrame (the prompt frame)
    output: OutputKind;
    // Mask output only: make the result part of this EXISTING object (group id) instead of the selected mask's
    // own one. Used when the user redrew a mask: the old object's masks are replaced, not left behind.
    targetGroup?: number | null;
}

async function stateOnFrame(job: Job, frame: number, clientID: number): Promise<ObjectState> {
    // allTracks = true: also return tracks that are currently `outside` (needed to bring a lost object back)
    const states = await job.annotations.get(frame, true, []);
    const found = states.find((s) => s.clientID === clientID);
    if (!found) throw new Error(`Object ${clientID} was not found on frame ${frame}`);
    return found;
}

/** Re-propagation of an existing polygon track: edit its keyframes in place, so its id is preserved. */
async function updateTrackInPlace(ctx: ApplyContext): Promise<string> {
    const { job, state } = ctx;
    const steps = planTrackUpdates(ctx.results, ctx.fromFrame, ctx.toFrame);
    for (const step of steps) {
        // eslint-disable-next-line no-await-in-loop
        const st = await stateOnFrame(job, step.frame, state.clientID as number);
        if (step.points === null) {
            st.outside = true;
        } else {
            st.points = step.points;
            st.outside = false;
        }
        st.keyframe = true;
        // eslint-disable-next-line no-await-in-loop
        await st.save();
    }

    const lastLost = steps.length > 0 && steps[steps.length - 1].points === null;
    const next = ctx.toFrame + 1;
    let marker = false;
    if (next <= ctx.stopFrame && !lastLost) {
        const after = await stateOnFrame(job, next, state.clientID as number);
        if (closingMarkerFrame({
            toFrame: ctx.toFrame, stopFrame: ctx.stopFrame, lastLost, nextHasKeyframe: after.keyframe,
        }) !== null) {
            after.outside = true;
            after.keyframe = true;
            await after.save();
            marker = true;
        }
    }
    const lost = steps.filter((s) => s.points === null).length;
    return `Updated polygon track ${state.serverID ?? '(unsaved)'}: frames ${ctx.fromFrame + 1}-${ctx.toFrame}`
        + `${lost ? `, object lost on ${lost} frame(s)` : ''}${marker ? `, closed on frame ${next}` : ''}.`;
}

/** First run from a mask/polygon SHAPE with polygon output: a new polygon track replaces the shape. */
async function createPolygonTrack(ctx: ApplyContext): Promise<string> {
    const { job, state } = ctx;
    const first = state.shapeType === 'polygon' ? state.points : ctx.results.get(ctx.fromFrame);
    if (!first || first.length < 6) {
        throw new Error('The selected mask has no usable outline to start a polygon track from');
    }
    // A track keeps immutable attributes on itself and mutable ones on each keyframe.
    const attrs = splitAttributes(state.attributes, state.label.attributes as { id: number; mutable: boolean }[]);
    const track = buildPolygonTrack({
        fromFrame: ctx.fromFrame,
        toFrame: ctx.toFrame,
        stopFrame: ctx.stopFrame,
        firstPoints: first,
        results: ctx.results,
        labelId: state.label.id,
        group: state.group?.id ?? 0,
        source: state.source,
        zOrder: state.zOrder,
        attributes: attrs.mutable,
        trackAttributes: attrs.immutable,
    });
    // Create first, delete the source shape afterwards: a failure in between leaves a duplicate, never a loss.
    // `import` takes the same serialized structures as the server API; the cast bridges core's looser typing.
    await job.annotations.import({ tracks: [track] } as unknown as Parameters<Job['annotations']['import']>[0]);
    await job.annotations.removeBatch([state], true);
    return `Created a polygon track with ${track.shapes.length} keyframes (frames ${ctx.fromFrame}-${ctx.toFrame}).`;
}

/** Mask output: one mask shape per frame, all in the object's group (the group id is the object identity). */
async function createMaskShapes(ctx: ApplyContext): Promise<string> {
    const { job, state } = ctx;
    const ownGroup = state.group?.id ?? 0;
    const attributes = serializeAttributes(state.attributes);
    const joining = !!ctx.targetGroup && ctx.targetGroup !== ownGroup;

    let groupId: number;
    if (joining) groupId = ctx.targetGroup as number;
    else if (ownGroup) groupId = ownGroup;
    else groupId = await job.annotations.group([state], false);

    // Replace earlier masks of this object in the range (re-propagation after a correction). When joining an
    // existing object, the old mask on the prompt frame itself is replaced by the user's new one as well.
    // removeBatch() is only valid for objects of ONE frame (core refuses mixed-frame history transactions),
    // so the stale masks are collected, and later removed, frame by frame.
    const stale = new Map<number, ObjectState[]>();
    let staleCount = 0;
    for (let frame = joining ? ctx.fromFrame : ctx.fromFrame + 1; frame <= ctx.toFrame; frame++) {
        // eslint-disable-next-line no-await-in-loop
        const states = await job.annotations.get(frame, false, []);
        const mine = states.filter((s) => s.objectType === 'shape' && s.shapeType === 'mask' && s.group?.id === groupId);
        if (mine.length) {
            stale.set(frame, mine);
            staleCount += mine.length;
        }
    }
    const shapes: object[] = buildMaskShapes({
        fromFrame: ctx.fromFrame,
        toFrame: ctx.toFrame,
        results: ctx.results,
        labelId: state.label.id,
        group: groupId,
        source: state.source,
        zOrder: state.zOrder,
        attributes,
    });
    if (joining) {
        // the user's redrawn mask itself joins the object: re-create it inside the group, drop the loose original
        shapes.unshift({
            type: 'mask', frame: ctx.fromFrame, label_id: state.label.id, group: groupId, source: state.source,
            occluded: state.occluded, outside: false, z_order: state.zOrder, rotation: 0, points: state.points,
            attributes,
        });
    }
    await job.annotations.import({ shapes } as unknown as Parameters<Job['annotations']['import']>[0]);
    for (const states of stale.values()) {
        // eslint-disable-next-line no-await-in-loop
        await job.annotations.removeBatch(states, true);
    }
    if (joining) await job.annotations.removeBatch([state], true);
    const created = shapes.length - (joining ? 1 : 0);
    return `Created ${created} mask shapes in group ${groupId} (frames ${ctx.fromFrame + 1}-${ctx.toFrame})`
        + `${staleCount ? `, replaced ${staleCount} earlier ones` : ''}${joining ? `; your mask joined object ${groupId}` : ''}.`;
}

export async function applyResults(ctx: ApplyContext): Promise<string> {
    if (ctx.state.objectType === 'track') return updateTrackInPlace(ctx);
    return ctx.output === 'polygon' ? createPolygonTrack(ctx) : createMaskShapes(ctx);
}

export interface DeleteContext {
    job: Job;
    state: ObjectState; // the selected object, as shown on `frame`
    frame: number;
    stopFrame: number;
    includeCurrent: boolean; // delete the current frame too (default) or only the frames after it
}

async function deleteTrackForward(ctx: DeleteContext, cut: number): Promise<string> {
    const { job, state } = ctx;
    const id = state.clientID as number;
    const here = await stateOnFrame(job, ctx.frame, id);
    const first = here.keyframes?.first;
    if (first === undefined || first === null || cut <= first) {
        await job.annotations.removeBatch([here], true); // nothing of the track would remain: remove the object
        return 'Deleted the whole track (it starts at or after the cut frame).';
    }
    if (cut > ctx.stopFrame) return 'Nothing follows this frame: nothing deleted.';

    // keyframes at or after the cut, found by walking the track's own keyframe links
    const later: number[] = [];
    let cursor = await stateOnFrame(job, cut - 1, id);
    for (;;) {
        const next = cursor.keyframes?.next;
        if (next === undefined || next === null) break;
        later.push(next);
        // eslint-disable-next-line no-await-in-loop
        cursor = await stateOnFrame(job, next, id);
    }
    // Remove the later keyframes one by one (the track is only deleted as a whole by core's delete()) ...
    for (const frame of later.filter((f) => f > cut)) {
        // eslint-disable-next-line no-await-in-loop
        const st = await stateOnFrame(job, frame, id);
        st.keyframe = false;
        // eslint-disable-next-line no-await-in-loop
        await st.save();
    }
    // ... and end the track on the cut frame (a keyframe is not removed on the cut itself: it becomes `outside`).
    const marker = await stateOnFrame(job, cut, id);
    let ended = false;
    if (!marker.outside) {
        marker.outside = true;
        marker.keyframe = true;
        await marker.save();
        ended = true;
    }
    return `Track now ends before frame ${cut}: removed ${later.filter((f) => f > cut).length} later keyframe(s)`
        + `${ended ? '' : ' (it was already ended there)'}; frames before ${cut} are untouched.`;
}

async function deleteMasksForward(ctx: DeleteContext, cut: number): Promise<string> {
    const { job, state } = ctx;
    const groupId = state.group?.id ?? 0;
    if (!groupId) {
        if (!ctx.includeCurrent) return 'This mask belongs to no object (group), so no other frame is affected.';
        await job.annotations.removeBatch([state], true);
        return 'Deleted this mask (it belongs to no object, so only this frame is affected).';
    }
    let removed = 0;
    for (let frame = cut; frame <= ctx.stopFrame; frame++) {
        // eslint-disable-next-line no-await-in-loop
        const states = await job.annotations.get(frame, false, []);
        const mine = states.filter((s) => s.objectType === 'shape' && s.shapeType === 'mask' && s.group?.id === groupId);
        if (mine.length) {
            // eslint-disable-next-line no-await-in-loop
            await job.annotations.removeBatch(mine, true); // one frame per call
            removed += mine.length;
        }
    }
    return `Deleted ${removed} mask(s) of object ${groupId} from frame ${cut} on; earlier frames are untouched.`;
}

/** "Delete from here": removes an object from the current frame (or the next one) to the end of the job. */
export async function deleteForward(ctx: DeleteContext): Promise<string> {
    const cut = deleteCutFrame(ctx.frame, ctx.includeCurrent);
    if (ctx.state.objectType === 'track') return deleteTrackForward(ctx, cut);
    if (ctx.state.shapeType === 'mask') return deleteMasksForward(ctx, cut);
    throw new Error(`Cannot delete a ${ctx.state.shapeType} ${ctx.state.objectType} this way.`);
}
