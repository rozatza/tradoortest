export function DslView({ source, errorLines }: { source: string; errorLines: Set<number> }) {
  const rows = source === "" ? [""] : source.split("\n");
  return (
    <ol className="dsl-view" aria-label="Strategy preview">
      {rows.map((row, index) => {
        const line = index + 1;
        const marked = errorLines.has(line);
        return (
          <li key={line} className={marked ? "line-error" : undefined} data-line={line}>
            <span className="line-no">{line}</span>
            <code>{row === "" ? " " : row}</code>
          </li>
        );
      })}
    </ol>
  );
}
