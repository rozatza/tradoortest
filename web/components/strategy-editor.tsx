"use client";

import { useEffect, useRef, useState } from "react";

import { DslView } from "@/components/dsl-view";
import { ProposalCard } from "@/components/proposal-card";
import {
  formatDiagnostic,
  listStrategies,
  loadStrategy,
  loadVersionText,
  loadVersions,
  saveStrategy,
  validateStrategy,
  type Diagnostic,
  type LoadedFile,
  type StrategySummary,
  type VersionSummary,
} from "@/lib/strategies";
import type { ProposalView } from "@/lib/types";

export function StrategyEditor({
  strategyId,
  refreshToken,
  proposals,
  busy,
  onAccept,
  onReject,
  onSelectStrategy,
}: {
  strategyId: string | null;
  refreshToken: number;
  proposals: ProposalView[];
  busy: boolean;
  onAccept: (proposalId: string) => void;
  onReject: (proposalId: string) => void;
  onSelectStrategy: (strategyId: string) => void;
}) {
  const [file, setFile] = useState<LoadedFile | null>(null);
  const [draft, setDraft] = useState("");
  const [versions, setVersions] = useState<VersionSummary[]>([]);
  const [strategies, setStrategies] = useState<StrategySummary[] | null>(null);
  const [diagnostics, setDiagnostics] = useState<Diagnostic[]>([]);
  const [conflict, setConflict] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [pendingVersion, setPendingVersion] = useState<number | null>(null);
  const [pendingStrategyId, setPendingStrategyId] = useState<string | null>(null);
  const [staleNote, setStaleNote] = useState(false);
  const [pending, setPending] = useState(false);
  const fileRef = useRef(file);
  const draftRef = useRef(draft);
  const refreshSeen = useRef(refreshToken);
  const generation = useRef(0);
  const lock = useRef(false);
  fileRef.current = file;
  draftRef.current = draft;

  useEffect(() => {
    if (!strategyId) {
      return;
    }
    const current = fileRef.current;
    const dirty = current !== null && draftRef.current !== current.dsl;
    const refreshChanged = refreshSeen.current !== refreshToken;
    refreshSeen.current = refreshToken;
    if (current && dirty && current.strategyId !== strategyId) {
      setPendingStrategyId(strategyId);
      return;
    }
    if (current && dirty && current.strategyId === strategyId && refreshChanged) {
      setStaleNote(true);
      return;
    }
    if (current && dirty && current.strategyId === strategyId) {
      return;
    }
    setPendingStrategyId(null);
    setStaleNote(false);
    void loadHead(strategyId);
  }, [strategyId, refreshToken]);

  async function loadHead(id: string) {
    const token = ++generation.current;
    setPending(true);
    setLoadError(null);
    try {
      const [loaded, history] = await Promise.all([loadStrategy(id), loadVersions(id)]);
      if (generation.current !== token) {
        return;
      }
      if (!loaded.ok) {
        setLoadError(failureLine(loaded));
        return;
      }
      setFile(loaded.file);
      setDraft(loaded.file.dsl);
      setDiagnostics([]);
      setConflict(null);
      setNote(null);
      setPendingVersion(null);
      setStaleNote(false);
      if (history.ok) {
        setVersions(history.versions);
      } else {
        setVersions([
          {
            version: loaded.file.viewedVersion,
            irHash: loaded.file.headHash,
            createdAt: "",
          },
        ]);
        setLoadError(failureLine(history));
      }
    } catch (error) {
      if (generation.current === token) {
        setLoadError(thrownLine(error));
      }
    } finally {
      if (generation.current === token) {
        setPending(false);
      }
    }
  }

  async function loadView(version: number) {
    if (!file) {
      return;
    }
    const token = ++generation.current;
    setPending(true);
    try {
      const loaded = await loadVersionText(file.strategyId, version);
      if (generation.current !== token) {
        return;
      }
      if (!loaded.ok) {
        setLoadError(failureLine(loaded));
        return;
      }
      setFile({ ...file, viewedVersion: loaded.version, dsl: loaded.dsl });
      setDraft(loaded.dsl);
      setDiagnostics([]);
      setConflict(null);
      setNote(null);
      setPendingVersion(null);
    } catch (error) {
      if (generation.current === token) {
        setLoadError(thrownLine(error));
      }
    } finally {
      if (generation.current === token) {
        setPending(false);
      }
    }
  }

  async function onShowSaved() {
    setPending(true);
    setLoadError(null);
    try {
      const listed = await listStrategies();
      if (!listed.ok) {
        setLoadError(failureLine(listed));
        return;
      }
      setStrategies(listed.strategies);
    } catch (error) {
      setLoadError(thrownLine(error));
    } finally {
      setPending(false);
    }
  }

  function onVersionPick(version: number) {
    if (!file || version === file.viewedVersion) {
      return;
    }
    if (draft !== file.dsl) {
      setPendingVersion(version);
      return;
    }
    void loadView(version);
  }

  async function onValidate() {
    if (!file || lock.current) {
      return;
    }
    lock.current = true;
    setPending(true);
    setConflict(null);
    setLoadError(null);
    try {
      const checked = await validateStrategy(file.strategyId, draft);
      if (!checked.ok) {
        showChecked(checked);
        return;
      }
      setDiagnostics([]);
      setNote("Valid. Nothing was saved.");
    } catch (error) {
      setDiagnostics([]);
      setNote(null);
      setLoadError(thrownLine(error));
    } finally {
      lock.current = false;
      setPending(false);
    }
  }

  async function onSave() {
    if (!file || lock.current) {
      return;
    }
    const text = draft;
    const headVersion = file.headVersion;
    const headHash = file.headHash;
    const id = file.strategyId;
    lock.current = true;
    setPending(true);
    setConflict(null);
    setLoadError(null);
    try {
      const checked = await validateStrategy(id, text);
      if (!checked.ok) {
        showChecked(checked);
        return;
      }
      setDiagnostics([]);
      const saved = await saveStrategy(id, text, headVersion, headHash);
      if (!saved.ok) {
        if (saved.status === 409 || saved.code === "version_conflict") {
          const current =
            saved.currentVersion !== null ? ` Current version is ${saved.currentVersion}.` : "";
          setConflict(`${saved.code}: ${saved.message}.${current} This edit was not saved.`);
          setNote(null);
          return;
        }
        showChecked(saved);
        return;
      }
      setFile(saved.file);
      setDraft(saved.file.dsl);
      setVersions((current) => mergeVersion(current, saved.file));
      setNote(`Saved version ${saved.file.viewedVersion}.`);
      setPendingVersion(null);
      setStaleNote(false);
    } catch (error) {
      setDiagnostics([]);
      setNote(null);
      setLoadError(thrownLine(error));
    } finally {
      lock.current = false;
      setPending(false);
    }
  }

  function showChecked(failure: { code: string; message: string; diagnostics: Diagnostic[] }) {
    if (failure.diagnostics.length > 0) {
      setDiagnostics(failure.diagnostics);
      setLoadError(null);
    } else {
      setDiagnostics([]);
      setLoadError(failureLine(failure));
    }
    setNote(null);
  }

  function keepEditing() {
    if (file) {
      onSelectStrategy(file.strategyId);
    }
    setPendingStrategyId(null);
  }

  async function discardAndOpen() {
    if (!pendingStrategyId) {
      return;
    }
    const next = pendingStrategyId;
    setPendingStrategyId(null);
    await loadHead(next);
  }

  const errorLines = new Set(
    diagnostics.map((item) => item.line).filter((line): line is number => line !== null),
  );
  const locked = pending || busy;

  return (
    <section className="editor" aria-label="Strategy file">
      <h2>Strategy file</h2>
      <p className="lede">
        Edit, validate, then save a new version. Nothing is written until you choose Save version.
      </p>
      {strategyId === null && strategies === null ? (
        <button type="button" onClick={() => void onShowSaved()} disabled={locked}>
          Show saved strategies
        </button>
      ) : null}
      {strategies ? (
        <label>
          Saved strategy
          <select
            aria-label="Saved strategy"
            value={strategyId ?? ""}
            onChange={(event) => {
              if (event.target.value !== "") {
                onSelectStrategy(event.target.value);
              }
            }}
          >
            <option value="">Choose a strategy</option>
            {strategies.map((item) => (
              <option key={item.strategyId} value={item.strategyId}>
                {item.name} v{item.latestVersion}
              </option>
            ))}
          </select>
        </label>
      ) : null}
      {pendingStrategyId ? (
        <p className="note">
          Unsaved edits are still in the editor.
          <span className="actions">
            <button type="button" onClick={() => void discardAndOpen()}>
              Discard and open
            </button>
            <button type="button" className="secondary" onClick={keepEditing}>
              Keep editing
            </button>
          </span>
        </p>
      ) : null}
      {file ? (
        <>
          <p className="meta">
            <span>{file.name || file.strategyId}</span>
            <span>Viewing v{file.viewedVersion}</span>
            <span>Head v{file.headVersion}</span>
          </p>
          <label>
            Version
            <select
              aria-label="Version"
              value={String(file.viewedVersion)}
              disabled={locked}
              onChange={(event) => onVersionPick(Number(event.target.value))}
            >
              {versions.map((item) => (
                <option key={item.version} value={item.version}>
                  v{item.version}
                </option>
              ))}
            </select>
          </label>
          {pendingVersion !== null ? (
            <p className="note">
              Load version {pendingVersion} and discard unsaved edits?
              <span className="actions">
                <button type="button" onClick={() => void loadView(pendingVersion)}>
                  Load version
                </button>
                <button type="button" className="secondary" onClick={() => setPendingVersion(null)}>
                  Keep editing
                </button>
              </span>
            </p>
          ) : null}
          <DslView source={draft} errorLines={errorLines} />
          <label htmlFor="strategy-dsl">Strategy DSL</label>
          <textarea
            id="strategy-dsl"
            aria-label="Strategy DSL"
            aria-invalid={diagnostics.length > 0}
            rows={12}
            value={draft}
            disabled={locked}
            onChange={(event) => {
              setDraft(event.target.value);
              setNote(null);
            }}
          />
          <div className="actions">
            <button type="button" onClick={() => void onValidate()} disabled={locked}>
              Validate
            </button>
            <button type="button" onClick={() => void onSave()} disabled={locked}>
              Save version
            </button>
          </div>
          {note ? <p className="status-line">{note}</p> : null}
          {diagnostics.length > 0 ? (
            <div className="diagnostics" role="alert">
              <ul>
                {diagnostics.map((item, index) => (
                  <li key={`${item.path ?? ""}-${item.line ?? "n"}-${index}`}>
                    {formatDiagnostic(item)}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
          {conflict ? (
            <p className="alert" role="alert">
              {conflict}
              <span className="actions">
                <button type="button" onClick={() => void loadHead(file.strategyId)}>
                  Reload latest
                </button>
              </span>
            </p>
          ) : null}
          {staleNote ? (
            <p className="alert" role="alert">
              The saved strategy changed. This editor was not overwritten.
              <span className="actions">
                <button type="button" onClick={() => void loadHead(file.strategyId)}>
                  Reload latest
                </button>
              </span>
            </p>
          ) : null}
          {proposals.map((proposal) => (
            <ProposalCard
              key={proposal.proposalId}
              proposal={proposal}
              disabled={locked}
              onAccept={onAccept}
              onReject={onReject}
            />
          ))}
        </>
      ) : (
        <p className="empty">Open a strategy to view and edit its file.</p>
      )}
      {loadError ? (
        <p className="alert" role="alert">
          {loadError}
        </p>
      ) : null}
    </section>
  );
}

function failureLine(failure: { code: string; message: string }): string {
  return `${failure.code}: ${failure.message}`;
}

function thrownLine(error: unknown): string {
  if (error instanceof Error && error.message !== "") {
    return `http_error: ${error.message}`;
  }
  return "http_error: the request could not be completed";
}

function mergeVersion(current: VersionSummary[], saved: LoadedFile): VersionSummary[] {
  if (current.some((item) => item.version === saved.viewedVersion)) {
    return current.map((item) =>
      item.version === saved.viewedVersion ? { ...item, irHash: saved.headHash } : item,
    );
  }
  return [
    ...current,
    { version: saved.viewedVersion, irHash: saved.headHash, createdAt: "" },
  ];
}
