// Copyright (C) 2026
//
// SPDX-License-Identifier: MIT

// Pure logic of the SAM 2 propagation plugin. It imports nothing from CVAT, so it is unit-tested with plain
// Node (see ../../test/plan.test.mjs). Everything that touches CVAT's core API lives in apply.ts.

export type OutputKind = 'mask' | 'polygon';

export const SERVICE_PATH = '/sam2/v1/video/propagate-job';

export interface Message {
    obj?: number; // multi-object streams: which object this result is for
    objects?: number; // multi-object `done` line: how many objects the service tracked
    frame?: number;
    points?: number[] | null;
    done?: boolean;
    frames?: number;
    output?: string;
    error?: string;
}

export interface PromptObject {
    id: number;
    prompt_type: OutputKind;
    prompt_points: number[];
}

export interface PropagateBody {
    job_id: number;
    org: string;
    from_frame: number;
    to_frame: number;
    prompt_type?: OutputKind; // single-object request ...
    prompt_points?: number[];
    objects?: PromptObject[]; // ... or several objects tracked together in one pass
    output: OutputKind;
    simplify_px: number;
}

/** Incremental parser for the service's newline-delimited JSON stream. */
export class NdjsonParser {
    private buffer = '';

    feed(chunk: string): Message[] {
        this.buffer += chunk;
        const lines = this.buffer.split('\n');
        this.buffer = lines.pop() ?? '';
        return lines.filter((line) => line.trim()).map((line) => JSON.parse(line) as Message);
    }

    flush(): Message[] {
        const rest = this.buffer.trim();
        this.buffer = '';
        return rest ? [JSON.parse(rest) as Message] : [];
    }
}

/** Which output formats make sense for the selected object. Mask TRACKS do not exist in the Community UI. */
export function allowedOutputs(objectType: string, shapeType: string): OutputKind[] {
    if (shapeType === 'polygon') return objectType === 'track' ? ['polygon'] : ['polygon', 'mask'];
    if (shapeType === 'mask' && objectType === 'shape') return ['mask', 'polygon'];
    return [];
}

export function unsupportedReason(objectType: string, shapeType: string): string {
    if (shapeType === 'mask' && objectType === 'track') {
        return 'Mask tracks cannot be used in CVAT Community. Select a mask shape or a polygon object.';
    }
    return `SAM 2 propagation supports mask and polygon objects; the selected object is a ${objectType} of type '${shapeType}'.`;
}

/** Mirror of CVAT's server-side rules, so bad service output is refused before anything is written. */
export function pointsAreValid(kind: OutputKind, points: number[] | null): boolean {
    if (points === null) return true; // "object not found" is a legitimate result
    return kind === 'polygon' ? points.length >= 6 && points.length % 2 === 0 : points.length >= 5;
}

export function buildBody(a: {
    jobId: number; org: string; fromFrame: number; toFrame: number; promptType: OutputKind;
    promptPoints: number[]; output: OutputKind; simplifyPx: number;
}): PropagateBody {
    return {
        job_id: a.jobId,
        org: a.org,
        from_frame: a.fromFrame,
        to_frame: a.toFrame,
        prompt_type: a.promptType,
        prompt_points: a.promptPoints,
        output: a.output,
        simplify_px: a.simplifyPx,
    };
}

/** Several objects in ONE request: the service downloads the frames once and tracks all of them in one pass. */
export function buildMultiBody(a: {
    jobId: number; org: string; fromFrame: number; toFrame: number;
    objects: { id: number; promptType: OutputKind; promptPoints: number[] }[];
    output: OutputKind; simplifyPx: number;
}): PropagateBody {
    return {
        job_id: a.jobId,
        org: a.org,
        from_frame: a.fromFrame,
        to_frame: a.toFrame,
        objects: a.objects.map((o) => ({ id: o.id, prompt_type: o.promptType, prompt_points: o.promptPoints })),
        output: a.output,
        simplify_px: a.simplifyPx,
    };
}

/**
 * One output format for the whole selection: the formats every selected object allows (a polygon track can
 * only stay a polygon track, so a selection containing one is polygon-only). Order follows the first object.
 */
export function commonOutputs(perObject: OutputKind[][]): OutputKind[] {
    if (!perObject.length) return [];
    return perObject[0].filter((kind) => perObject.every((list) => list.includes(kind)));
}

/** The service numbers frames relative to the prompt frame (0 = prompt). Returns absolute job frame numbers. */
export function toAbsolute(relative: Map<number, number[] | null>, fromFrame: number): Map<number, number[] | null> {
    const out = new Map<number, number[] | null>();
    relative.forEach((points, rel) => out.set(fromFrame + rel, points));
    return out;
}

/** Human-readable text for a failed HTTP response from /sam2/... */
export function errorText(status: number, bodyText: string): string {
    let detail = '';
    try {
        const parsed = JSON.parse(bodyText);
        const d = parsed.detail ?? parsed.message ?? parsed.error;
        detail = typeof d === 'string' ? d : JSON.stringify(d);
    } catch {
        detail = bodyText.trim().slice(0, 200);
    }
    if (status === 401 || status === 403) return `Not allowed (HTTP ${status}). ${detail}`.trim();
    if ((status === 404 || status === 405) && !bodyText.trim().startsWith('{')) {
        // An HTML answer (CVAT's UI nginx, or Traefik's own 404) means the request never reached our service.
        return `The /sam2 route is not active in CVAT's Traefik (HTTP ${status} from the web server, not from `
            + 'sam2-service). Recreate sam2-service so that its Traefik labels are applied: '
            + 'docker compose -f docker-compose.sam2.yml up -d --force-recreate';
    }
    if (status === 422) {
        return `The service rejected the request (HTTP 422: ${detail}). It may be outdated; rebuild sam2-service.`;
    }
    if (status === 429) return 'The service is busy with another propagation; try again in a moment.';
    if (status === 502 || status === 503 || status === 504) {
        // Our own service also answers 502 when CVAT misbehaves; its JSON message says what happened.
        if (bodyText.trim().startsWith('{') && detail) return `SAM 2 service error (HTTP ${status}): ${detail}`;
        return `The SAM 2 service is not reachable through CVAT's proxy (HTTP ${status}).`;
    }
    return `HTTP ${status}: ${detail}`;
}

/**
 * A CVAT track keeps showing its last keyframe until the end of the job unless an `outside` keyframe ends it.
 * Returns the frame for a closing `outside` keyframe, or null when none is needed (end of job, the range
 * already ends outside, or a real keyframe already follows).
 */
export function closingMarkerFrame(a: {
    toFrame: number; stopFrame: number; lastLost: boolean; nextHasKeyframe: boolean;
}): number | null {
    const next = a.toFrame + 1;
    if (next > a.stopFrame || a.lastLost || a.nextHasKeyframe) return null;
    return next;
}

/** In-place track update: one entry per frame in (fromFrame, toFrame]; points === null -> outside keyframe. */
export function planTrackUpdates(
    results: Map<number, number[] | null>, fromFrame: number, toFrame: number,
): { frame: number; points: number[] | null }[] {
    const steps = [];
    for (let frame = fromFrame + 1; frame <= toFrame; frame++) {
        if (!results.has(frame)) throw new Error(`The service returned no result for frame ${frame}`);
        steps.push({ frame, points: results.get(frame) ?? null });
    }
    return steps;
}

export interface SerializedAttribute { spec_id: number; value: string }

/**
 * CVAT keeps IMMUTABLE attributes on the track itself and only MUTABLE ones on each keyframe; shapes keep all of
 * theirs on the shape. Splits ObjectState.attributes accordingly (specs: the label's attribute definitions).
 */
export function splitAttributes(
    attributes: Record<number, string> | undefined | null,
    specs: { id: number; mutable: boolean }[],
): { immutable: SerializedAttribute[]; mutable: SerializedAttribute[] } {
    const mutableIds = new Set(specs.filter((sp) => sp.mutable).map((sp) => sp.id));
    const all = serializeAttributes(attributes);
    return {
        immutable: all.filter((a) => !mutableIds.has(a.spec_id)),
        mutable: all.filter((a) => mutableIds.has(a.spec_id)),
    };
}

/** ObjectState.attributes ({specId: value}) -> the serialized form used by import(). */
export function serializeAttributes(attributes: Record<number, string> | undefined | null): SerializedAttribute[] {
    return Object.entries(attributes ?? {}).map(([id, value]) => ({ spec_id: Number(id), value: String(value) }));
}

export interface SerializedPolygonShape {
    type: 'polygon'; frame: number; points: number[]; outside: boolean; occluded: boolean;
    z_order: number; rotation: number; attributes: SerializedAttribute[];
}

export interface SerializedTrack {
    frame: number; label_id: number; group: number; source: string; attributes: SerializedAttribute[];
    shapes: SerializedPolygonShape[];
}

function polygonShape(
    frame: number, points: number[], outside: boolean, zOrder: number, attributes: SerializedAttribute[] = [],
): SerializedPolygonShape {
    return {
        type: 'polygon', frame, points: [...points], outside, occluded: false, z_order: zOrder, rotation: 0,
        attributes: attributes.map((a) => ({ ...a })),
    };
}

/** A brand-new polygon track: the user's own polygon on fromFrame, then one keyframe per propagated frame. */
export function buildPolygonTrack(a: {
    fromFrame: number; toFrame: number; stopFrame: number; firstPoints: number[];
    results: Map<number, number[] | null>; labelId: number; group: number; source: string; zOrder: number;
    attributes?: SerializedAttribute[]; // mutable attributes: repeated on every keyframe
    trackAttributes?: SerializedAttribute[]; // immutable attributes: stored once, on the track
}): SerializedTrack {
    const attrs = a.attributes ?? [];
    const shapes = [polygonShape(a.fromFrame, a.firstPoints, false, a.zOrder, attrs)];
    let last = a.firstPoints;
    let lastLost = false;
    for (const { frame, points } of planTrackUpdates(a.results, a.fromFrame, a.toFrame)) {
        lastLost = points === null;
        if (points !== null) last = points;
        shapes.push(polygonShape(frame, last, lastLost, a.zOrder, attrs));
    }
    const marker = closingMarkerFrame({
        toFrame: a.toFrame, stopFrame: a.stopFrame, lastLost, nextHasKeyframe: false,
    });
    if (marker !== null) shapes.push(polygonShape(marker, last, true, a.zOrder, attrs));
    return {
        frame: a.fromFrame, label_id: a.labelId, group: a.group, source: a.source,
        attributes: (a.trackAttributes ?? []).map((x) => ({ ...x })), shapes,
    };
}

export interface SerializedMaskShape {
    type: 'mask'; frame: number; label_id: number; group: number; source: string; occluded: boolean;
    outside: boolean; z_order: number; rotation: number; points: number[]; attributes: SerializedAttribute[];
}

/** One mask shape per frame in (fromFrame, toFrame] where the object was found, all in group `group`. */
export function buildMaskShapes(a: {
    fromFrame: number; toFrame: number; results: Map<number, number[] | null>; labelId: number;
    group: number; source: string; zOrder: number; attributes?: SerializedAttribute[];
}): SerializedMaskShape[] {
    return planTrackUpdates(a.results, a.fromFrame, a.toFrame)
        .filter((s) => s.points !== null)
        .map((s) => ({
            type: 'mask', frame: s.frame, label_id: a.labelId, group: a.group, source: a.source,
            occluded: false, outside: false, z_order: a.zOrder, rotation: 0, points: s.points as number[],
            attributes: (a.attributes ?? []).map((x) => ({ ...x })),
        }));
}

export interface LooseShape { type: string; group?: number; frame: number; label_id: number }

export interface GroupInfo { id: number; labelId: number; first: number; last: number; count: number }

/** Mask objects (= groups of mask shapes) present in the job, for the "replace existing object" choice. */
export function summarizeGroups(shapes: LooseShape[]): GroupInfo[] {
    const byGroup = new Map<number, GroupInfo>();
    shapes.forEach((s) => {
        const id = s.group ?? 0;
        if (s.type !== 'mask' || id <= 0) return;
        const g = byGroup.get(id);
        if (!g) byGroup.set(id, { id, labelId: s.label_id, first: s.frame, last: s.frame, count: 1 });
        else {
            g.first = Math.min(g.first, s.frame);
            g.last = Math.max(g.last, s.frame);
            g.count += 1;
        }
    });
    return [...byGroup.values()].sort((a, b) => a.id - b.id);
}

/**
 * When the selected mask has no object of its own (it was just drawn), guess which existing object it is the
 * correction of: the single other group with the same label that has a mask on the previous, same or next frame.
 * Returns null when there is no, or more than one, candidate -- the user then chooses explicitly.
 */
export function suggestGroup(shapes: LooseShape[], frame: number, labelId: number, ownGroup: number): number | null {
    const near = new Set<number>();
    shapes.forEach((s) => {
        const id = s.group ?? 0;
        if (s.type === 'mask' && id > 0 && id !== ownGroup && s.label_id === labelId && Math.abs(s.frame - frame) <= 1) {
            near.add(id);
        }
    });
    return near.size === 1 ? [...near][0] : null;
}

/**
 * "Delete from here": the frame from which the object is removed. `includeCurrent` = the frame the user is on is
 * deleted too (default); otherwise only the following frames.
 */
export function deleteCutFrame(frame: number, includeCurrent: boolean): number {
    return includeCurrent ? frame : frame + 1;
}
