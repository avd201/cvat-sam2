// Copyright (C) 2026
// SPDX-License-Identifier: MIT

import type { Job, ObjectState } from 'cvat-core-wrapper';
import { splitAttributes } from './plan';

/** Tight continuous-coordinate bounds; mask pixel (x,y) occupies [x,x+1] × [y,y+1]. */
export function tightBox(kind: string, points: number[]): number[] {
    if (!points.every(Number.isFinite)) throw new Error('Non-finite annotation coordinates.');
    if (kind === 'polygon') {
        if (points.length < 6 || points.length % 2) throw new Error('Invalid polygon points.');
        let left = Infinity;
        let top = Infinity;
        let right = -Infinity;
        let bottom = -Infinity;
        for (let i = 0; i < points.length; i += 2) {
            left = Math.min(left, points[i]);
            top = Math.min(top, points[i + 1]);
            right = Math.max(right, points[i]);
            bottom = Math.max(bottom, points[i + 1]);
        }
        if (left >= right || top >= bottom) throw new Error('Polygon has a zero-width or zero-height box.');
        return [left, top, right, bottom];
    }
    if (kind !== 'mask' || points.length < 5 || !points.every(Number.isSafeInteger)) {
        throw new Error('Invalid mask points.');
    }
    const [x, y, right, bottom] = points.slice(-4);
    const width = right - x + 1;
    const height = bottom - y + 1;
    const area = width * height;
    if (x < 0 || y < 0 || width < 1 || height < 1 || !Number.isSafeInteger(area)) {
        throw new Error('Invalid mask crop.');
    }
    let offset = 0;
    let left = width;
    let top = height;
    let maxX = -1;
    let maxY = -1;
    // Scan runs rather than expanding the bitmap. A foreground run crossing a row boundary touches both edges.
    for (let i = 0; i < points.length - 4; i++) {
        const count = points[i];
        if (count < 0 || count > area - offset) throw new Error('Invalid mask run length.');
        if (i % 2 && count) {
            const end = offset + count - 1;
            const firstY = Math.floor(offset / width);
            const lastY = Math.floor(end / width);
            left = Math.min(left, firstY === lastY ? offset % width : 0);
            maxX = Math.max(maxX, firstY === lastY ? end % width : width - 1);
            top = Math.min(top, firstY);
            maxY = Math.max(maxY, lastY);
        }
        offset += count;
    }
    if (offset !== area || maxX < 0) throw new Error('Empty or incomplete mask RLE.');
    return [x + left, y + top, x + maxX + 1, y + maxY + 1];
}

interface BoxKeyframe {
    type: 'rectangle'; frame: number; points: number[]; outside: boolean; occluded: boolean;
    rotation: number; z_order: number; attributes: { spec_id: number; value: string }[];
}
export interface BoxTrack {
    label_id: number; group: number; source: string; frame: number;
    attributes: { spec_id: number; value: string }[]; shapes: BoxKeyframe[];
}
interface Target { state: ObjectState; key: string; shapes: BoxKeyframe[]; immutable: string | null }

export function boxSourceKey(state: ObjectState): string {
    if (state.objectType === 'track' && state.shapeType === 'polygon') return `track:${state.clientID}`;
    if (state.objectType === 'shape' && state.shapeType === 'mask') {
        return state.group?.id ? `mask-group:${state.group.id}` : `mask:${state.clientID}`;
    }
    throw new Error('Select polygon tracks or mask shapes (a grouped mask selects its whole object).');
}

/** Read every frame through CVAT so polygon interpolation and mutable attributes match its canvas. No writes here. */
async function prepareBoxTracks(
    job: Job, selected: ObjectState[], report: (done: number, total: number) => void = () => {},
    signal?: AbortSignal,
): Promise<{ tracks: BoxTrack[]; sources: Map<number, ObjectState[]> }> {
    const targets: Target[] = [];
    for (const state of selected) {
        const key = boxSourceKey(state);
        if (!targets.some((t) => t.key === key)) targets.push({ state, key, shapes: [], immutable: null });
    }
    if (!targets.length) throw new Error('Select at least one polygon track or mask object.');
    const sources = new Map<number, ObjectState[]>();
    const sourceIDs = new Set<number>();
    const total = job.stopFrame - job.startFrame + 1;
    for (let frame = job.startFrame; frame <= job.stopFrame; frame++) {
        if (signal?.aborted) throw new Error('Conversion cancelled; nothing was changed.');
        // eslint-disable-next-line no-await-in-loop
        const states = await job.annotations.get(frame, true, []);
        for (const target of targets) {
            const candidates = states.filter((s) => {
                if (target.state.objectType === 'track') {
                    return s.objectType === 'track' && s.clientID === target.state.clientID
                        && frame >= (s.keyframes?.first ?? job.startFrame);
                }
                return s.objectType === 'shape' && s.shapeType === 'mask'
                    && (target.state.group?.id ? s.group?.id === target.state.group.id : s.clientID === target.state.clientID);
            });
            if (candidates.length > 1) {
                throw new Error(`${target.key}, frame ${frame}: several masks share this group. Separate them before conversion.`);
            }
            const [state] = candidates;
            if (state && !sourceIDs.has(state.clientID as number)) {
                sourceIDs.add(state.clientID as number);
                const batch = sources.get(frame) ?? [];
                batch.push(state);
                sources.set(frame, batch);
            }
            const previous = target.shapes[target.shapes.length - 1];
            if (!state || state.outside) {
                if (previous && !previous.outside) target.shapes.push({ ...previous, frame, outside: true });
                continue;
            }
            if (state.label.id !== target.state.label.id) throw new Error(`${target.key}: the mask group contains different labels.`);
            const attrs = splitAttributes(state.attributes, state.label.attributes as { id: number; mutable: boolean }[]);
            // One track cannot carry changing immutable values: refuse instead of silently losing them.
            const immutable = JSON.stringify([...attrs.immutable].sort((a, b) => a.spec_id - b.spec_id));
            if (target.immutable !== null && immutable !== target.immutable) {
                throw new Error(`${target.key}: immutable attributes differ between masks.`);
            }
            target.immutable = immutable;
            let points: number[];
            try {
                points = tightBox(state.shapeType, state.points);
            } catch (error) {
                throw new Error(`${target.key}, frame ${frame}: ${(error as Error).message}`);
            }
            target.shapes.push({
                type: 'rectangle', frame, points, outside: false, rotation: 0,
                occluded: state.occluded, z_order: state.zOrder, attributes: attrs.mutable,
            });
        }
        report(frame - job.startFrame + 1, total);
        // Let the browser paint progress and process cancellation during long jobs.
        if ((frame - job.startFrame) % 20 === 0) {
            // eslint-disable-next-line no-await-in-loop
            await new Promise<void>((resolve) => { setTimeout(resolve, 0); });
        }
    }
    if (signal?.aborted) throw new Error('Conversion cancelled; nothing was changed.');
    const tracks = targets.map(({ state, key, shapes, immutable }) => {
        if (!shapes.length) throw new Error(`${key}: no visible frames in this job.`);
        return {
            label_id: state.label.id, group: state.group?.id ?? 0, source: state.source,
            frame: shapes[0].frame, attributes: JSON.parse(immutable as string), shapes,
        };
    });
    return { tracks, sources };
}

export async function planBoxTracks(
    job: Job, selected: ObjectState[], report: (done: number, total: number) => void = () => {},
    signal?: AbortSignal,
): Promise<BoxTrack[]> {
    return (await prepareBoxTracks(job, selected, report, signal)).tracks;
}

/** Create boxes before removing any originals. Source batches contain only the planned source identities. */
export async function convertToBoxTracks(
    job: Job, selected: ObjectState[], replaceOriginals = false,
    report: (done: number, total: number) => void = () => {}, signal?: AbortSignal,
): Promise<string> {
    const { tracks, sources } = await prepareBoxTracks(job, selected, report, signal);
    if (replaceOriginals && [...sources.values()].some((batch) => batch.some((s) => s.isGroundTruth))) {
        throw new Error('Ground-truth reference annotations cannot be replaced. Nothing was changed.');
    }
    if (signal?.aborted) throw new Error('Conversion cancelled; nothing was changed.');
    let summary: string;
    try {
        summary = await importBoxTracks(job, tracks);
    } catch (error) {
        throw new Error(`Box creation failed: ${(error as Error).message}. No source annotations were removed.`);
    }
    if (!replaceOriginals) return summary;
    let removed = 0;
    const total = [...sources.values()].reduce((count, batch) => count + batch.length, 0);
    try {
        // Once boxes exist, complete replacement without cancellation between source batches.
        // CVAT history transactions require one frame per removeBatch call.
        for (const batch of sources.values()) {
            // eslint-disable-next-line no-await-in-loop
            const ids = await job.annotations.removeBatch(batch, true);
            removed += ids.length;
            if (batch.some((state) => !ids.includes(state.clientID as number))) {
                throw new Error('CVAT did not remove every source annotation in a batch');
            }
        }
    } catch (error) {
        throw new Error(`Created ${tracks.length} box track(s), but replacing originals stopped after ${removed}/${total}`
            + ` source annotations: ${(error as Error).message}. Inspect the job before saving; the boxes were kept.`);
    }
    return `Created ${tracks.length} tight box track(s) and replaced their original polygons/masks.`
        + ' Boxes have new CVAT track IDs. Press Ctrl+S to save.';
}

export async function importBoxTracks(job: Job, tracks: BoxTrack[]): Promise<string> {
    // No source IDs on the new tracks/keyframes; CVAT allocates their new identities.
    await job.annotations.import({ tracks } as unknown as Parameters<Job['annotations']['import']>[0]);
    return `Created ${tracks.length} tight box track(s). Original polygons and masks were kept. Press Ctrl+S to save.`;
}
