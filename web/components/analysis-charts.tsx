"use client";

import { chartNotes, type AnalysisPayload } from "@/lib/charts";

export function AnalysisCharts({ analysis }: { analysis: AnalysisPayload }) {
  if (!analysis.available) {
    return (
      <div className="chart-block">
        <h3>Time of day</h3>
        <p>Time of day unavailable</p>
        <h3>Weekday</h3>
        <p>Weekday unavailable</p>
        <Usage analysis={analysis} />
      </div>
    );
  }
  const hourNotes = chartNotes(
    analysis.hours.length,
    analysis.hours.length + analysis.dropped,
    analysis.hours.length,
    analysis.hours.length,
    "hours shown",
  );
  return (
    <div className="chart-block">
      <h3>Time of day</h3>
      {analysis.hours.length === 0 ? (
        <p>Time of day unavailable</p>
      ) : (
        <BucketChart
          label="UTC hour of entry"
          buckets={analysis.hours.map((bucket) => ({
            key: String(bucket.hourUtc),
            name: `${String(bucket.hourUtc).padStart(2, "0")} UTC`,
            count: bucket.count,
          }))}
        />
      )}
      <h3>Weekday</h3>
      {analysis.weekdays.length === 0 ? (
        <p>Weekday unavailable</p>
      ) : (
        <BucketChart
          label="UTC weekday of entry"
          buckets={analysis.weekdays.map((bucket) => ({
            key: String(bucket.weekdayUtc),
            name: bucket.weekday,
            count: bucket.count,
          }))}
        />
      )}
      {hourNotes.length ? <p className="bar-readout">{hourNotes.join(" · ")}</p> : null}
      <Usage analysis={analysis} />
    </div>
  );
}

function Usage({ analysis }: { analysis: AnalysisPayload }) {
  if (analysis.tradesUsed < analysis.tradesTotal) {
    return (
      <p className="bar-readout">
        {analysis.tradesUsed} of {analysis.tradesTotal} trades used
      </p>
    );
  }
  if (analysis.reason) {
    return <p>{analysis.reason}</p>;
  }
  return null;
}

function BucketChart({
  label,
  buckets,
}: {
  label: string;
  buckets: { key: string; name: string; count: number }[];
}) {
  let max = 1;
  for (const bucket of buckets) {
    if (bucket.count > max) {
      max = bucket.count;
    }
  }
  return (
    <div className="buckets" aria-label={label}>
      {buckets.map((bucket) => (
        <div key={bucket.key} className="bucket">
          <div className="bucket-bar" style={{ height: `${Math.max(4, (bucket.count / max) * 72)}px` }} />
          <div className="bucket-count">{bucket.count}</div>
          <div className="bucket-name">{bucket.name}</div>
        </div>
      ))}
    </div>
  );
}
