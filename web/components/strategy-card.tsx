import type { StrategyView } from "@/lib/types";

export function StrategyCard({
  strategy,
  onOpen,
}: {
  strategy: StrategyView;
  onOpen: (strategyId: string) => void;
}) {
  const title = strategy.action === "written" ? "Strategy written" : "Strategy updated";
  return (
    <section className="card strategy" aria-label={title}>
      <h2>{title}</h2>
      {strategy.name ? <p className="strategy-name">{strategy.name}</p> : null}
      <dl>
        <div>
          <dt>Strategy</dt>
          <dd>{strategy.strategyId}</dd>
        </div>
        {strategy.version !== null ? (
          <div>
            <dt>Version</dt>
            <dd>{strategy.version}</dd>
          </div>
        ) : null}
        {strategy.irHash ? (
          <div>
            <dt>Hash</dt>
            <dd>{strategy.irHash}</dd>
          </div>
        ) : null}
      </dl>
      <div className="actions">
        <button type="button" onClick={() => onOpen(strategy.strategyId)}>
          Open
        </button>
      </div>
    </section>
  );
}
