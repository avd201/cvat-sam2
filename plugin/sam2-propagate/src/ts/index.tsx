// Copyright (C) 2026
//
// SPDX-License-Identifier: MIT

import React from 'react';
import { createRoot, Root } from 'react-dom/client';
import Modal from 'antd/lib/modal';
import { ComponentBuilder, PluginEntryPoint } from 'components/plugins-entrypoint';
import { Job, ObjectState } from 'cvat-core-wrapper';
import {
    LooseShape, OutputKind, allowedOutputs, buildBody, buildMultiBody, commonOutputs, pointsAreValid, suggestGroup,
    summarizeGroups, toAbsolute, unsupportedReason,
} from './plan';
import { runPropagation, runPropagationMulti } from './service';
import { applyResults, deleteForward } from './apply';
import { PLUGIN_VERSION } from './version';
import Sam2Dialog, {
    DeleteDialog, ObjectChoice, ProgressReporter, RunConfig,
} from './ui';

const MENU_PATH = 'annotationPage.menuActions.items';
const SIMPLIFY_PX = 1.5;
const TITLE = 'SAM 2';

interface Selection {
    job: Job;
    frame: number;
    states: ObjectState[];
}

const describe = (state: ObjectState): string => {
    const kind = state.objectType === 'track' ? `${state.shapeType} track` : `${state.shapeType} shape`;
    return `${kind} “${state.label.name}” (id ${state.serverID ?? `new-${state.clientID}`})`;
};

const builder: ComponentBuilder = ({
    actionCreators, dispatch, core, store,
}) => {
    // eslint-disable-next-line no-console
    console.info(`[SAM 2 plugin] version ${PLUGIN_VERSION} loaded`);
    let root: Root | null = null;
    let host: HTMLDivElement | null = null;

    const closeDialog = (): void => {
        root?.unmount();
        host?.remove();
        root = null;
        host = null;
    };

    const show = (element: JSX.Element): void => {
        host = document.createElement('div');
        document.body.appendChild(host);
        root = createRoot(host);
        root.render(element);
    };

    // The objects to work on: the selected objects, else the one under the mouse (activated). All of them are on
    // the current frame, which is therefore the common prompt frame.
    const resolveSelection = (): Selection | string => {
        const { annotation } = store.getState();
        const job = annotation.job.instance as Job | null | undefined;
        if (!job) return 'Open a job first.';
        const { selectedStatesID, activatedStateID, states } = annotation.annotations;
        let ids: number[] = [];
        if (selectedStatesID.length) ids = [...selectedStatesID];
        else if (activatedStateID !== null) ids = [activatedStateID];
        if (!ids.length) {
            return 'Select one or more objects (click them, or hover over one and use the shortcut).';
        }
        const found = ids
            .map((id) => (states as ObjectState[]).find((s) => s.clientID === id))
            .filter((s): s is ObjectState => !!s);
        if (found.length !== ids.length) return 'Some selected objects are not on the current frame.';
        return { job, frame: annotation.player.frame.number as number, states: found };
    };

    const refresh = (frame: number): void => {
        // Re-read the current frame's annotations so the canvas and the objects list show the new state.
        dispatch(actionCreators.changeFrameAsync(frame, false, 1, true));
    };

    const labelName = (job: Job, labelId: number): string => (
        job.labels.find((l) => l.id === labelId)?.name ?? `label ${labelId}`
    );

    const openPropagate = async (): Promise<void> => {
        if (root) return; // a dialog is already open
        const selection = resolveSelection();
        if (typeof selection === 'string') {
            Modal.info({ title: TITLE, content: selection });
            return;
        }
        const { job, frame, states } = selection;
        if (frame >= job.stopFrame) {
            Modal.warning({ title: TITLE, content: 'This is the last frame of the job: nothing to propagate.' });
            return;
        }

        // Check EVERY selected object before anything runs.
        const problems: string[] = [];
        const perObject = states.map((st) => {
            const outs = allowedOutputs(st.objectType, st.shapeType);
            if (!outs.length) problems.push(`${describe(st)}: ${unsupportedReason(st.objectType, st.shapeType)}`);
            if (st.outside) problems.push(`${describe(st)}: marked outside on this frame.`);
            return outs;
        });
        if (problems.length) {
            Modal.warning({
                title: TITLE,
                content: <span style={{ whiteSpace: 'pre-line' }}>{problems.join('\n')}</span>,
            });
            return;
        }
        const outputs = commonOutputs(perObject);
        if (!outputs.length) {
            Modal.warning({
                title: TITLE,
                content: 'These objects need different output types. Select them separately.',
            });
            return;
        }

        const multi = states.length > 1;
        const [first] = states;
        // Mask objects already in the job: a redrawn mask can REPLACE one instead of leaving it behind.
        // (Offered for a single selected mask only.)
        const ownGroup = multi ? 0 : (first.group?.id ?? 0);
        let choices: ObjectChoice[] = [];
        let suggested: number | null = null;
        if (!multi && first.objectType === 'shape') {
            try {
                const exported = await job.annotations.export();
                const shapes = (exported.shapes ?? []) as unknown as LooseShape[];
                choices = summarizeGroups(shapes)
                    .filter((g) => g.id !== ownGroup)
                    .map((g) => ({
                        id: g.id,
                        text: `object ${g.id} (${labelName(job, g.labelId)}, frames ${g.first}-${g.last})`,
                    }));
                if (!ownGroup) suggested = suggestGroup(shapes, frame, first.label.id, ownGroup);
            } catch {
                choices = []; // the choice is a convenience; propagation works without it
            }
        }

        const org = (core as any).config?.organization?.organizationSlug ?? '';

        const run = async (config: RunConfig, report: ProgressReporter, signal: AbortSignal): Promise<string> => {
            const total = config.toFrame - frame + 1;
            const invalid = (res: Map<number, number[] | null>): [number, number[] | null] | undefined => (
                [...res.entries()].find(([, p]) => !pointsAreValid(config.output, p))
            );

            if (!multi) {
                const body = buildBody({
                    jobId: job.id,
                    org,
                    fromFrame: frame,
                    toFrame: config.toFrame,
                    promptType: first.shapeType as OutputKind,
                    promptPoints: first.points as number[],
                    output: config.output,
                    simplifyPx: SIMPLIFY_PX,
                });
                const relative = await runPropagation(body, (count) => report('propagating', count, total), signal);
                const results = toAbsolute(relative, frame);
                const broken = invalid(results);
                if (broken) {
                    throw new Error(`The service returned invalid ${config.output} data on frame ${broken[0]}; nothing was changed.`);
                }
                report('applying', total, total);
                const summary = await applyResults({
                    job,
                    state: first,
                    fromFrame: frame,
                    toFrame: config.toFrame,
                    stopFrame: job.stopFrame,
                    results,
                    output: config.output,
                    targetGroup: config.targetGroup,
                });
                refresh(frame);
                return summary;
            }

            // Several objects: ONE request (the frames are fetched and loaded once, all objects tracked in one pass).
            const body = buildMultiBody({
                jobId: job.id,
                org,
                fromFrame: frame,
                toFrame: config.toFrame,
                objects: states.map((st, i) => ({
                    id: i + 1, promptType: st.shapeType as OutputKind, promptPoints: st.points as number[],
                })),
                output: config.output,
                simplifyPx: SIMPLIFY_PX,
            });
            const perObj = await runPropagationMulti(
                body, (count) => report('propagating', count, total * states.length), signal,
            );
            // Validate every object's results BEFORE writing any of them.
            const resultsFor = states.map((_, i) => toAbsolute(perObj.get(i + 1) as Map<number, number[] | null>, frame));
            for (let i = 0; i < states.length; i++) {
                const broken = invalid(resultsFor[i]);
                if (broken) {
                    throw new Error(
                        `The service returned invalid ${config.output} data for ${describe(states[i])} on frame ${broken[0]}; nothing was changed.`,
                    );
                }
            }
            report('applying', total, total);
            const applied: string[] = [];
            for (let i = 0; i < states.length; i++) {
                try {
                    // eslint-disable-next-line no-await-in-loop
                    const summary = await applyResults({
                        job,
                        state: states[i],
                        fromFrame: frame,
                        toFrame: config.toFrame,
                        stopFrame: job.stopFrame,
                        results: resultsFor[i],
                        output: config.output,
                        targetGroup: null,
                    });
                    applied.push(`${describe(states[i])}: ${summary}`);
                } catch (error) {
                    refresh(frame); // show what was written so far
                    throw new Error(
                        `${describe(states[i])} failed: ${(error as Error).message}\n`
                        + `${applied.length ? `Already applied:\n${applied.join('\n')}` : 'Nothing was changed.'}`,
                    );
                }
            }
            refresh(frame);
            return `Propagated ${states.length} objects.\n${applied.join('\n')}`;
        };

        show(
            <Sam2Dialog
                objectLines={states.map((st) => `${describe(st)} on frame ${frame}`)}
                objectCount={states.length}
                fromFrame={frame}
                stopFrame={job.stopFrame}
                outputs={outputs}
                ownGroup={ownGroup}
                choices={choices}
                suggested={suggested}
                run={run}
                onClose={closeDialog}
            />,
        );
    };

    const openDelete = (): void => {
        if (root) return;
        const selection = resolveSelection();
        if (typeof selection === 'string') {
            Modal.info({ title: TITLE, content: selection });
            return;
        }
        const { job, frame, states } = selection;
        const unsupported = states.filter((st) => st.objectType !== 'track' && st.shapeType !== 'mask');
        if (unsupported.length) {
            Modal.warning({
                title: TITLE,
                content: (
                    <span style={{ whiteSpace: 'pre-line' }}>
                        {`Delete-from-here works on tracks and mask objects. Use CVAT's own Delete for:\n${unsupported.map(describe).join('\n')}`}
                    </span>
                ),
            });
            return;
        }
        const tracks = states.filter((st) => st.objectType === 'track').length;
        let kind: 'track' | 'mask' | 'mixed' = 'mixed';
        if (tracks === states.length) kind = 'track';
        else if (tracks === 0) kind = 'mask';

        show(
            <DeleteDialog
                objectLines={states.map((st) => `${describe(st)} on frame ${frame}`)}
                frame={frame}
                kind={kind}
                hasGroup={states.every((st) => (st.group?.id ?? 0) > 0)}
                run={async (includeCurrent) => {
                    const done: string[] = [];
                    for (const st of states) {
                        try {
                            // eslint-disable-next-line no-await-in-loop
                            const message = await deleteForward({
                                job, state: st, frame, stopFrame: job.stopFrame, includeCurrent,
                            });
                            done.push(states.length > 1 ? `${describe(st)}: ${message}` : message);
                        } catch (error) {
                            refresh(frame);
                            throw new Error(
                                `${describe(st)} failed: ${(error as Error).message}\n`
                                + `${done.length ? `Already done:\n${done.join('\n')}` : 'Nothing was changed.'}`,
                            );
                        }
                    }
                    refresh(frame);
                    return done.join('\n');
                }}
                onClose={closeDialog}
            />,
        );
    };

    const propagateItem = ({ key }: { key: string | number }) => ({
        key: `sam2-propagate-${key}`,
        label: 'SAM 2: propagate object(s)…',
        onClick: () => { openPropagate(); },
    });
    const deleteItem = ({ key }: { key: string | number }) => ({
        key: `sam2-delete-${key}`,
        label: 'SAM 2: delete object(s) from here…',
        onClick: openDelete,
    });
    dispatch(actionCreators.addUIComponent(MENU_PATH, propagateItem as any, { weight: 15, shouldBeRendered: () => true }));
    dispatch(actionCreators.addUIComponent(MENU_PATH, deleteItem as any, { weight: 16, shouldBeRendered: () => true }));

    const onKeyDown = (event: KeyboardEvent): void => {
        if (!(event.altKey && event.shiftKey)) return;
        if (event.code !== 'KeyP' && event.code !== 'KeyD') return;
        const el = event.target as HTMLElement | null;
        if (el && (['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName) || el.isContentEditable)) return;
        event.preventDefault();
        if (event.code === 'KeyP') openPropagate();
        else openDelete();
    };
    window.addEventListener('keydown', onKeyDown);

    return {
        name: 'sam2-propagate',
        destructor: () => {
            window.removeEventListener('keydown', onKeyDown);
            dispatch(actionCreators.removeUIComponent(MENU_PATH, propagateItem as any));
            dispatch(actionCreators.removeUIComponent(MENU_PATH, deleteItem as any));
            closeDialog();
        },
    };
};

function register(): void {
    if (Object.prototype.hasOwnProperty.call(window, 'cvatUI')) {
        (window as any as { cvatUI: { registerComponent: PluginEntryPoint } }).cvatUI.registerComponent(builder);
    }
}

window.addEventListener('plugins.ready', register, { once: true });
