import { LiveConfirm } from "@/components/live-confirm";

export default function LivePage() {
  return (
    <main>
      <header>
        <p className="eyebrow">Live / manual confirm</p>
        <h1>Sign a trade</h1>
        <p className="lede">
          The server builds an unsigned swap, guards it, and shows the decoded trade. You sign it
          in your own wallet. Network, priority, and rent are separate from the notional. This
          page does not hold a key.
        </p>
        <p>
          <a href="/">Author workspace</a>
          {" · "}
          <a href="/paper">PAPER dashboard</a>
        </p>
      </header>
      <LiveConfirm />
    </main>
  );
}
