import { PaperDashboard } from "@/components/paper-dashboard";

export default function PaperPage() {
  return (
    <main className="paper">
      <header>
        <p className="eyebrow">PAPER / simulated</p>
        <h1>Paper dashboard</h1>
        <p className="lede">
          Running paper strategies, positions, stored realized P&L, a labeled mark, and the event
          log. This screen does not send an order.
        </p>
        <p>
          <a href="/">Author workspace</a>
        </p>
      </header>
      <PaperDashboard />
    </main>
  );
}
