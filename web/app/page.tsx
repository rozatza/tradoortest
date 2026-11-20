import { Chat } from "@/components/chat";

export default function HomePage() {
  return (
    <main>
      <header>
        <p className="eyebrow">Internal tool</p>
        <h1>tradoortest</h1>
        <p className="lede">
          Author chat, strategy file, and backtest results for one local user. This screen does not trade.
        </p>
      </header>
      <Chat />
    </main>
  );
}
