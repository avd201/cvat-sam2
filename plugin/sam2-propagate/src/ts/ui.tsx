// Copyright (C) 2026
//
// SPDX-License-Identifier: MIT

import React, { useRef, useState } from 'react';
import Modal from 'antd/lib/modal';
import Radio from 'antd/lib/radio';
import Select from 'antd/lib/select';
import InputNumber from 'antd/lib/input-number';
import Progress from 'antd/lib/progress';
import Alert from 'antd/lib/alert';
import Button from 'antd/lib/button';
import { OutputKind } from './plan';

export interface RunConfig {
    toFrame: number;
    output: OutputKind;
    targetGroup: number | null; // mask output: join this existing object instead of the selected mask's own one
}

export type ProgressReporter = (phase: 'propagating' | 'applying', done: number, total: number) => void;

export interface ObjectChoice {
    id: number;
    text: string; // e.g. "object 3 (cow, frames 0-40)"
}

export interface DialogProps {
    objectLines: string[]; // one line per selected object
    objectCount: number;
    fromFrame: number;
    stopFrame: number;
    outputs: OutputKind[];
    ownGroup: number; // the selected mask's own object (0 = none yet)
    choices: ObjectChoice[]; // other mask objects the result could replace
    suggested: number | null; // pre-selected choice (a neighbouring object with the same label), or null
    run: (config: RunConfig, report: ProgressReporter, signal: AbortSignal) => Promise<string>;
    onClose: () => void;
}

const OUTPUT_LABELS: Record<OutputKind, string> = {
    mask: 'Mask shapes (one per frame, grouped)',
    polygon: 'Polygon track',
};

export default function Sam2Dialog(props: DialogProps): JSX.Element {
    const {
        objectLines, objectCount, fromFrame, stopFrame, outputs, ownGroup, choices, suggested, run, onClose,
    } = props;
    const [toFrame, setToFrame] = useState<number>(Math.min(fromFrame + 30, stopFrame));
    const [output, setOutput] = useState<OutputKind>(outputs[0]);
    // 0 = the selected mask's own object (or a new one if it has none); otherwise an existing object's id
    const [target, setTarget] = useState<number>(ownGroup ? 0 : (suggested ?? 0));
    const [phase, setPhase] = useState<'config' | 'propagating' | 'applying' | 'done' | 'error'>('config');
    const [progress, setProgress] = useState<{ done: number; total: number }>({ done: 0, total: 1 });
    const [message, setMessage] = useState<string>('');
    const abort = useRef<AbortController | null>(null);

    const busy = phase === 'propagating' || phase === 'applying';
    const locked = busy || phase === 'done';
    const valid = Number.isInteger(toFrame) && toFrame > fromFrame && toFrame <= stopFrame;
    const showObjectChoice = output === 'mask' && choices.length > 0;

    const start = async (): Promise<void> => {
        const controller = new AbortController();
        abort.current = controller;
        setPhase('propagating');
        setMessage('');
        setProgress({ done: 0, total: toFrame - fromFrame + 1 });
        try {
            const summary = await run({
                toFrame, output, targetGroup: output === 'mask' && target ? target : null,
            }, (p, done, total) => {
                setPhase(p);
                setProgress({ done, total });
            }, controller.signal);
            setMessage(summary);
            setPhase('done');
        } catch (error) {
            setMessage((error as Error).message);
            setPhase('error');
        }
    };

    const close = (): void => {
        if (busy) abort.current?.abort();
        onClose();
    };

    const ownText = ownGroup ? `This mask's own object (${ownGroup})` : 'A new object';

    return (
        <Modal
            open
            title={objectCount > 1 ? `SAM 2: propagate ${objectCount} objects` : 'SAM 2: propagate object'}
            onCancel={close}
            maskClosable={false}
            footer={[
                <Button key='close' onClick={close}>{busy ? 'Cancel' : 'Close'}</Button>,
                phase !== 'done' ? (
                    <Button key='run' type='primary' disabled={!valid || busy} onClick={start}>
                        Propagate
                    </Button>
                ) : null,
            ]}
        >
            <ObjectList lines={objectLines} />
            <p>
                Starting from frame
                {' '}
                <b>{fromFrame}</b>
                {' '}
                (current frame), through frame:
                {' '}
                <InputNumber
                    min={fromFrame + 1}
                    max={stopFrame}
                    value={toFrame}
                    disabled={locked}
                    onChange={(value) => setToFrame(Number(value))}
                />
                {' '}
                (job ends at
                {' '}
                {stopFrame}
                )
            </p>
            {outputs.length > 1 ? (
                <Radio.Group value={output} disabled={locked} onChange={(e) => setOutput(e.target.value as OutputKind)}>
                    {outputs.map((o) => <Radio key={o} value={o}>{OUTPUT_LABELS[o]}</Radio>)}
                </Radio.Group>
            ) : (
                <p>
                    Output:
                    {' '}
                    {OUTPUT_LABELS[outputs[0]]}
                </p>
            )}
            {showObjectChoice ? (
                <div style={{ marginTop: 12 }}>
                    <div>Result belongs to:</div>
                    <Select
                        style={{ width: '100%' }}
                        value={target}
                        disabled={locked}
                        onChange={(value: number) => setTarget(value)}
                        options={[
                            { value: 0, label: ownText },
                            ...choices.map((c) => ({ value: c.id, label: `Replace ${c.text}` })),
                        ]}
                    />
                    {target ? (
                        <div style={{ marginTop: 4 }}>
                            This mask joins that object; its masks on frames
                            {' '}
                            {fromFrame}
                            –
                            {toFrame}
                            {' '}
                            are replaced.
                        </div>
                    ) : null}
                </div>
            ) : null}
            {busy ? (
                <div style={{ marginTop: 12 }}>
                    <Progress percent={Math.round((100 * progress.done) / Math.max(progress.total, 1))} status='active' />
                    <div>
                        {phase === 'applying'
                            ? 'Writing results into the job…'
                            : `Tracking ${objectCount > 1 ? `${objectCount} objects` : 'the object'} with SAM 2…`}
                    </div>
                </div>
            ) : null}
            {phase === 'done' ? (
                <Alert
                    style={{ marginTop: 12 }}
                    type='success'
                    showIcon
                    message={<span style={{ whiteSpace: 'pre-line' }}>{message}</span>}
                    description='Press Ctrl+S to save the job.'
                />
            ) : null}
            {phase === 'error' ? (
                <Alert style={{ marginTop: 12 }} type='error' showIcon message={<span style={{ whiteSpace: 'pre-line' }}>{message}</span>} />
            ) : null}
        </Modal>
    );
}

function ObjectList(props: { lines: string[] }): JSX.Element {
    const { lines } = props;
    if (lines.length === 1) return <p>{lines[0]}</p>;
    return (
        <div>
            <p>Selected objects:</p>
            <ul style={{ maxHeight: 120, overflowY: 'auto' }}>
                {lines.map((line) => <li key={line}>{line}</li>)}
            </ul>
        </div>
    );
}

export interface DeleteDialogProps {
    objectLines: string[];
    frame: number;
    kind: 'track' | 'mask' | 'mixed';
    hasGroup: boolean; // masks only: whether the mask belongs to an object (group)
    run: (includeCurrent: boolean) => Promise<string>;
    onClose: () => void;
}

export function DeleteDialog(props: DeleteDialogProps): JSX.Element {
    const {
        objectLines, frame, kind, hasGroup, run, onClose,
    } = props;
    const [includeCurrent, setIncludeCurrent] = useState<boolean>(true);
    const [phase, setPhase] = useState<'config' | 'running' | 'done' | 'error'>('config');
    const [message, setMessage] = useState<string>('');

    const go = async (): Promise<void> => {
        setPhase('running');
        try {
            setMessage(await run(includeCurrent));
            setPhase('done');
        } catch (error) {
            setMessage((error as Error).message);
            setPhase('error');
        }
    };

    return (
        <Modal
            open
            title='SAM 2: delete object from here'
            onCancel={onClose}
            maskClosable={false}
            footer={[
                <Button key='close' onClick={onClose}>{phase === 'done' ? 'Close' : 'Cancel'}</Button>,
                phase !== 'done' ? (
                    <Button key='go' danger type='primary' loading={phase === 'running'} onClick={go}>
                        Delete
                    </Button>
                ) : null,
            ]}
        >
            <ObjectList lines={objectLines} />
            <Radio.Group
                value={includeCurrent}
                disabled={phase === 'running' || phase === 'done'}
                onChange={(e) => setIncludeCurrent(e.target.value as boolean)}
            >
                <Radio style={{ display: 'block' }} value>
                    {`This frame (${frame}) and all following frames`}
                </Radio>
                <Radio style={{ display: 'block' }} value={false}>
                    {`Only the frames after ${frame} (keep this one)`}
                </Radio>
            </Radio.Group>
            <p style={{ marginTop: 8 }}>
                {kind === 'track' && 'Tracks end there; earlier frames are not changed.'}
                {kind === 'mask' && (hasGroup
                    ? 'The masks of the object on those frames are removed; earlier frames and other objects are not changed.'
                    : 'This mask belongs to no object, so only this frame can be affected.')}
                {kind === 'mixed' && 'Tracks end there and mask objects lose their masks from there on; earlier frames and unselected objects are not changed.'}
            </p>
            {phase === 'done' ? (
                <Alert type='success' showIcon message={<span style={{ whiteSpace: 'pre-line' }}>{message}</span>} description='Press Ctrl+S to save the job.' />
            ) : null}
            {phase === 'error' ? <Alert type='error' showIcon message={message} /> : null}
        </Modal>
    );
}
