// Copyright (C) 2026
//
// SPDX-License-Identifier: MIT

import {
    Message, NdjsonParser, PropagateBody, SERVICE_PATH, errorText,
} from './plan';

export class ServiceError extends Error {}

/**
 * POST the request to the SAM 2 service (through CVAT's own address, so the user's session cookie is sent) and
 * feed every NDJSON line to `onMessage`. Resolves with the final {"done":true} line; rejects if the stream
 * errors, is interrupted, or ends without it (so a half-finished run is never mistaken for a complete one).
 */
async function streamMessages(
    body: PropagateBody,
    onMessage: (message: Message) => void,
    signal: AbortSignal,
): Promise<Message> {
    let response: Response;
    try {
        response = await fetch(SERVICE_PATH, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'same-origin',
            body: JSON.stringify(body),
            signal,
        });
    } catch (error) {
        if (signal.aborted) throw new ServiceError('Cancelled');
        throw new ServiceError(`Cannot reach the SAM 2 service: ${(error as Error).message}`);
    }

    if (!response.ok) throw new ServiceError(errorText(response.status, await response.text()));
    if (!response.body) throw new ServiceError('This browser does not support streaming responses');

    let done: Message | null = null;
    let received = 0;
    const handle = (message: Message): void => {
        if (message.error) throw new ServiceError(`Service error after ${received} results: ${message.error}`);
        if (typeof message.frame === 'number') {
            received += 1;
            onMessage(message);
        }
        if (message.done) done = message;
    };

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    const parser = new NdjsonParser();
    try {
        for (;;) {
            // eslint-disable-next-line no-await-in-loop
            const { value, done: finished } = await reader.read();
            if (finished) break;
            parser.feed(decoder.decode(value, { stream: true })).forEach(handle);
        }
        parser.flush().forEach(handle);
    } catch (error) {
        if (error instanceof ServiceError) throw error;
        if (signal.aborted) throw new ServiceError('Cancelled');
        throw new ServiceError(`The stream was interrupted after ${received} results: ${(error as Error).message}`);
    }

    const finalMessage = done as Message | null;
    if (finalMessage === null) throw new ServiceError(`The stream ended without completing (${received} results received)`);
    if ((finalMessage.output ?? 'mask') !== body.output) {
        throw new ServiceError(
            `Asked for '${body.output}' output but the service produced '${finalMessage.output ?? 'mask'}'; `
            + 'the service is outdated and must be rebuilt.',
        );
    }
    return finalMessage;
}

/** One object. Resolves with results keyed by frame number RELATIVE to the prompt frame (0 = the prompt). */
export async function runPropagation(
    body: PropagateBody,
    onFrame: (count: number) => void,
    signal: AbortSignal,
): Promise<Map<number, number[] | null>> {
    const relative = new Map<number, number[] | null>();
    await streamMessages(body, (m) => {
        relative.set(m.frame as number, m.points ?? null);
        onFrame(relative.size);
    }, signal);
    return relative;
}

/**
 * Several objects tracked together in ONE request. Resolves with object id -> (relative frame -> points).
 * Refuses a service that ignored `objects` (an older service would have tracked nothing or only one object).
 */
export async function runPropagationMulti(
    body: PropagateBody,
    onResult: (count: number) => void,
    signal: AbortSignal,
): Promise<Map<number, Map<number, number[] | null>>> {
    const expected = body.objects?.length ?? 0;
    const byObject = new Map<number, Map<number, number[] | null>>();
    let count = 0;
    const finalMessage = await streamMessages(body, (m) => {
        if (typeof m.obj !== 'number') throw new ServiceError('The service answered without object ids; it is outdated and must be rebuilt.');
        if (!byObject.has(m.obj)) byObject.set(m.obj, new Map());
        (byObject.get(m.obj) as Map<number, number[] | null>).set(m.frame as number, m.points ?? null);
        count += 1;
        onResult(count);
    }, signal);
    if (finalMessage.objects !== expected || byObject.size !== expected) {
        throw new ServiceError(
            `The service tracked ${byObject.size} of ${expected} objects; nothing was changed.`,
        );
    }
    return byObject;
}
