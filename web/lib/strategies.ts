export type Diagnostic = {
  code: string;
  message: string;
  path: string | null;
  line: number | null;
  column: number | null;
};

export type StrategySummary = {
  strategyId: string;
  name: string;
  latestVersion: number;
};

export type VersionSummary = {
  version: number;
  irHash: string;
  createdAt: string;
};

export type LoadedFile = {
  strategyId: string;
  name: string;
  headVersion: number;
  headHash: string;
  viewedVersion: number;
  dsl: string;
};

export type FileFailure = {
  ok: false;
  status: number;
  code: string;
  message: string;
  diagnostics: Diagnostic[];
  currentVersion: number | null;
  currentIrHash: string | null;
};

export function formatDiagnostic(item: Diagnostic): string {
  const where =
    item.line !== null && item.column !== null
      ? `Line ${item.line}, column ${item.column}: `
      : item.line !== null
        ? `Line ${item.line}: `
        : item.path
          ? `${item.path}: `
          : "";
  return `${where}${item.message}`;
}

export async function listStrategies(): Promise<
  { ok: true; strategies: StrategySummary[] } | FileFailure
> {
  const response = await fetch("/api/strategies?limit=100", {
    headers: { accept: "application/json" },
    cache: "no-store",
  });
  if (!response.ok) {
    return readFailure(response);
  }
  const body = (await response.json()) as { strategies?: unknown };
  return { ok: true, strategies: readSummaries(body.strategies) };
}

export async function loadStrategy(strategyId: string): Promise<
  { ok: true; file: LoadedFile } | FileFailure
> {
  const response = await fetch(`/api/strategies/${strategyId}`, {
    headers: { accept: "application/json" },
    cache: "no-store",
  });
  if (!response.ok) {
    return readFailure(response);
  }
  const file = readDetail(await response.json());
  if (!file) {
    return invalidResponse(response.status);
  }
  return { ok: true, file };
}

export async function loadVersions(strategyId: string): Promise<
  { ok: true; versions: VersionSummary[] } | FileFailure
> {
  const versions: VersionSummary[] = [];
  let offset = 0;
  let total = 0;
  do {
    const response = await fetch(
      `/api/strategies/${strategyId}/versions?limit=100&offset=${offset}`,
      { headers: { accept: "application/json" }, cache: "no-store" },
    );
    if (!response.ok) {
      return readFailure(response);
    }
    const body = (await response.json()) as { versions?: unknown; total?: unknown };
    const page = readVersionPage(body.versions);
    total = typeof body.total === "number" && Number.isFinite(body.total) ? body.total : page.length;
    versions.push(...page);
    if (page.length === 0) {
      break;
    }
    offset += page.length;
  } while (offset < total && offset <= 10_000);
  return { ok: true, versions };
}

export async function loadVersionText(
  strategyId: string,
  version: number,
): Promise<{ ok: true; version: number; dsl: string; irHash: string } | FileFailure> {
  const response = await fetch(`/api/strategies/${strategyId}/versions/${version}`, {
    headers: { accept: "application/json" },
    cache: "no-store",
  });
  if (!response.ok) {
    return readFailure(response);
  }
  const body = (await response.json()) as Record<string, unknown>;
  const number = numberField(body, "version");
  const dsl = typeof body.dsl === "string" ? body.dsl : null;
  const irHash = stringField(body, "ir_hash");
  if (number === null || dsl === null || irHash === null) {
    return invalidResponse(response.status);
  }
  return { ok: true, version: number, dsl, irHash };
}

export async function validateStrategy(
  strategyId: string,
  dsl: string,
): Promise<{ ok: true } | FileFailure> {
  const response = await fetch(`/api/strategies/${strategyId}/validate`, {
    method: "POST",
    headers: { "content-type": "application/json", accept: "application/json" },
    body: JSON.stringify({ dsl }),
    cache: "no-store",
  });
  if (!response.ok) {
    return readFailure(response);
  }
  const text = await response.text();
  if (text.trim() !== "") {
    try {
      JSON.parse(text);
    } catch {
      return {
        ok: false,
        status: response.status,
        code: "invalid_response",
        message: "the author API returned an invalid response",
        diagnostics: [],
        currentVersion: null,
        currentIrHash: null,
      };
    }
  }
  return { ok: true };
}

export async function saveStrategy(
  strategyId: string,
  dsl: string,
  expectedVersion: number,
  expectedIrHash: string,
): Promise<{ ok: true; file: LoadedFile } | FileFailure> {
  const response = await fetch(`/api/strategies/${strategyId}`, {
    method: "PUT",
    headers: { "content-type": "application/json", accept: "application/json" },
    body: JSON.stringify({
      dsl,
      expected_version: expectedVersion,
      expected_ir_hash: expectedIrHash,
    }),
    cache: "no-store",
  });
  if (!response.ok) {
    return readFailure(response);
  }
  let parsed: unknown;
  try {
    parsed = await response.json();
  } catch {
    return {
      ok: false,
      status: response.status,
      code: "invalid_response",
      message: "the author API returned an invalid response",
      diagnostics: [],
      currentVersion: null,
      currentIrHash: null,
    };
  }
  const file = readDetail(parsed);
  if (!file) {
    return invalidResponse(response.status);
  }
  return { ok: true, file };
}

function readDetail(value: unknown): LoadedFile | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }
  const record = value as Record<string, unknown>;
  const strategyId = stringField(record, "strategy_id");
  const name = stringField(record, "name") ?? "";
  const headVersion = numberField(record, "latest_version");
  const headHash = stringField(record, "ir_hash");
  const version = record.version;
  if (
    strategyId === null ||
    headVersion === null ||
    headHash === null ||
    !version ||
    typeof version !== "object" ||
    Array.isArray(version)
  ) {
    return null;
  }
  const versionRecord = version as Record<string, unknown>;
  const dsl = typeof versionRecord.dsl === "string" ? versionRecord.dsl : null;
  const viewedVersion = numberField(versionRecord, "version");
  if (dsl === null || viewedVersion === null) {
    return null;
  }
  return { strategyId, name, headVersion, headHash, viewedVersion, dsl };
}

function readSummaries(value: unknown): StrategySummary[] {
  if (!Array.isArray(value)) {
    return [];
  }
  const summaries: StrategySummary[] = [];
  for (const item of value) {
    if (!item || typeof item !== "object" || Array.isArray(item)) {
      continue;
    }
    const record = item as Record<string, unknown>;
    const strategyId = stringField(record, "strategy_id");
    const latestVersion = numberField(record, "latest_version");
    if (strategyId === null || latestVersion === null) {
      continue;
    }
    summaries.push({
      strategyId,
      name: stringField(record, "name") ?? strategyId,
      latestVersion,
    });
  }
  return summaries;
}

function readVersionPage(value: unknown): VersionSummary[] {
  if (!Array.isArray(value)) {
    return [];
  }
  const versions: VersionSummary[] = [];
  for (const item of value) {
    if (!item || typeof item !== "object" || Array.isArray(item)) {
      continue;
    }
    const record = item as Record<string, unknown>;
    const version = numberField(record, "version");
    const irHash = stringField(record, "ir_hash");
    if (version === null || irHash === null) {
      continue;
    }
    versions.push({
      version,
      irHash,
      createdAt: stringField(record, "created_at") ?? "",
    });
  }
  return versions;
}

async function readFailure(response: Response): Promise<FileFailure> {
  try {
    const body = (await response.json()) as { error?: unknown };
    const error = body.error;
    if (error && typeof error === "object" && !Array.isArray(error)) {
      const record = error as Record<string, unknown>;
      const message = typeof record.message === "string" ? record.message : "";
      if (message !== "") {
        return {
          ok: false,
          status: response.status,
          code: typeof record.code === "string" && record.code !== "" ? record.code : "http_error",
          message,
          diagnostics: readDiagnostics(record),
          currentVersion: numberField(record, "current_version"),
          currentIrHash: stringField(record, "current_ir_hash"),
        };
      }
    }
  } catch {
    // The body was not the JSON error envelope.
  }
  return {
    ok: false,
    status: response.status,
    code: "http_error",
    message: `request failed (${response.status})`,
    diagnostics: [],
    currentVersion: null,
    currentIrHash: null,
  };
}

function readDiagnostics(error: Record<string, unknown>): Diagnostic[] {
  const raw = error.diagnostics;
  const parsed: Diagnostic[] = [];
  if (Array.isArray(raw)) {
    for (const item of raw) {
      const diagnostic = readDiagnostic(item);
      if (diagnostic) {
        parsed.push(diagnostic);
      }
    }
  }
  if (parsed.length > 0) {
    return parsed;
  }
  const message = typeof error.message === "string" ? error.message : "";
  if (message === "") {
    return [];
  }
  return [
    {
      code: typeof error.code === "string" && error.code !== "" ? error.code : "error",
      message,
      path: typeof error.path === "string" ? error.path : null,
      line: null,
      column: null,
    },
  ];
}

function readDiagnostic(value: unknown): Diagnostic | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }
  const record = value as Record<string, unknown>;
  const message = typeof record.message === "string" ? record.message : "";
  if (message === "") {
    return null;
  }
  return {
    code: typeof record.code === "string" && record.code !== "" ? record.code : "error",
    message,
    path: typeof record.path === "string" ? record.path : null,
    line: numberField(record, "line"),
    column: numberField(record, "column"),
  };
}

function invalidResponse(status: number): FileFailure {
  return {
    ok: false,
    status,
    code: "invalid_response",
    message: "the author API returned an invalid strategy",
    diagnostics: [],
    currentVersion: null,
    currentIrHash: null,
  };
}

function stringField(record: Record<string, unknown>, key: string): string | null {
  const value = record[key];
  return typeof value === "string" && value !== "" ? value : null;
}

function numberField(record: Record<string, unknown>, key: string): number | null {
  const value = record[key];
  return typeof value === "number" && Number.isInteger(value) ? value : null;
}
