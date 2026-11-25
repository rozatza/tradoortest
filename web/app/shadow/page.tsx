"use client";

import { useEffect, useState } from "react";

import { ShadowQuotes } from "@/components/paper-dashboard";
import { EMPTY_SHADOW, errorMessage, readShadowReport, type ShadowReport } from "@/lib/paper";

export default function ShadowPage() {
  const [report, setReport] = useState<ShadowReport>(EMPTY_SHADOW);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const pull = async () => {
      try {
        const response = await fetch("/api/shadow", {
          cache: "no-store",
          headers: { accept: "application/json" },
        });
        const body: unknown = await response.json();
        if (cancelled) {
          return;
        }
        if (!response.ok) {
          setError(errorMessage(body, "the shadow API is unavailable"));
          return;
        }
        setReport(readShadowReport(body));
        setError(null);
      } catch {
        if (!cancelled) {
          setError("the shadow API is unavailable");
        }
      }
    };
    void pull();
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <main className="paper">
      <header>
        <p className="eyebrow">PAPER / simulated</p>
        <h1>Shadow quotes</h1>
        <p className="lede">
          A Jupiter quote for the trade a paper fill would have made. No order is signed or sent.
          Network, priority, and rent stay off strategy P&L.
        </p>
        <p>
          <a href="/paper">Paper dashboard</a>
        </p>
      </header>
      {error ? (
        <p className="alert" role="alert">
          {error}
        </p>
      ) : null}
      <ShadowQuotes report={report} />
    </main>
  );
}
