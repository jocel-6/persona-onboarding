"use client";

import { useCallback, useEffect, useState } from "react";
import { API_URL } from "@/lib/api";

type Metrics = {
  kpis: {
    sessions: number;
    graduated_pct: number | null;
    voice_first_audio_p50: number | null;
    voice_first_audio_p90: number | null;
    llm_first_token_p50: number | null;
    llm_first_token_p90: number | null;
    cost_per_conversation: number | null;
    cache_hit_pct: number | null;
    turns: number;
    voice_turns_with_audio: number;
  };
  funnel: { step: string; count: number; drop_pct: number | null }[];
  voice_latency_hist: { from: number; to: number | null; count: number }[];
  by_model: { model: string; turns: number; ttft_p50: number | null; cost_per_turn: number | null }[];
};

const RANGES = [
  { days: 1, label: "Last day" },
  { days: 7, label: "7 days" },
  { days: 30, label: "30 days" },
];

const ms = (v: number | null) => (v == null ? "–" : v >= 1000 ? `${(v / 1000).toFixed(2)} s` : `${v} ms`);
const usd = (v: number | null) => (v == null ? "–" : v < 0.01 ? `${(v * 100).toFixed(2)}¢` : `$${v.toFixed(3)}`);
const binLabel = (b: { from: number; to: number | null }) => (b.to == null ? `${b.from / 1000}s+` : `${b.from}–${b.to}`);

export default function MetricsPage() {
  const [days, setDays] = useState(30);
  const [data, setData] = useState<Metrics | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (d: number) => {
    setError(null);
    try {
      const token = new URLSearchParams(window.location.search).get("token") ?? "";
      const r = await fetch(`${API_URL}/api/metrics?days=${d}&token=${encodeURIComponent(token)}`);
      if (!r.ok) throw new Error(r.status === 401 ? "This dashboard needs ?token=… in the URL." : `HTTP ${r.status}`);
      setData(await r.json());
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn't load metrics.");
    }
  }, []);

  useEffect(() => {
    const t = setTimeout(() => void load(days), 0);
    return () => clearTimeout(t);
  }, [days, load]);

  const k = data?.kpis;
  return (
    <main className="metrics">
      <header className="metrics-head">
        <div>
          <h1>Persona metrics</h1>
          <p className="muted small">Onboarding funnel, speed and cost. Counts and timings only, never conversation content.</p>
        </div>
        <div className="range" role="group" aria-label="Time range">
          {RANGES.map((r) => (
            <button key={r.days} className={`chip ${days === r.days ? "on" : ""}`} onClick={() => setDays(r.days)} aria-pressed={days === r.days}>
              {r.label}
            </button>
          ))}
        </div>
      </header>

      {error && <div className="banner error">{error}</div>}

      {k && (
        <>
          <section className="tiles">
            <Tile label="Conversations" value={k.sessions.toLocaleString()} note={`${k.turns.toLocaleString()} turns`} />
            <Tile label="Got into the app" value={k.graduated_pct == null ? "–" : `${k.graduated_pct}%`} />
            <Tile label="Voice response" value={ms(k.voice_first_audio_p50)} note={`p90 ${ms(k.voice_first_audio_p90)}`} />
            <Tile label="Claude first token" value={ms(k.llm_first_token_p50)} note={`p90 ${ms(k.llm_first_token_p90)}`} />
            <Tile label="Cost per conversation" value={usd(k.cost_per_conversation)} note="Claude only" />
            <Tile label="Prompt cache hits" value={k.cache_hit_pct == null ? "–" : `${k.cache_hit_pct}%`} note="of input tokens" />
          </section>

          <ChartCard title="Onboarding funnel" subtitle="Conversations reaching each step, and the drop-off from the step before">
            {(table) => (table ? <FunnelTable rows={data.funnel} /> : <Funnel rows={data.funnel} />)}
          </ChartCard>

          <ChartCard
            title="Voice response time"
            subtitle={`From the end of your turn to the agent's first sound, over ${k.voice_turns_with_audio} call turns`}
          >
            {(table) =>
              table ? (
                <HistTable bins={data.voice_latency_hist} />
              ) : (
                <Histogram bins={data.voice_latency_hist} p50={k.voice_first_audio_p50} p90={k.voice_first_audio_p90} />
              )
            }
          </ChartCard>

          <section className="card metrics-card">
            <h2>By model</h2>
            <table className="mtable">
              <thead>
                <tr><th>Model</th><th>Turns</th><th>First token (median)</th><th>Cost per turn</th></tr>
              </thead>
              <tbody>
                {data.by_model.map((m) => (
                  <tr key={m.model}>
                    <td>{m.model}</td>
                    <td>{m.turns.toLocaleString()}</td>
                    <td>{ms(m.ttft_p50)}</td>
                    <td>{usd(m.cost_per_turn)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
        </>
      )}
    </main>
  );
}

function Tile({ label, value, note }: { label: string; value: string; note?: string }) {
  return (
    <div className="tile">
      <span className="tile-label">{label}</span>
      <span className="tile-value">{value}</span>
      {note && <span className="tile-note">{note}</span>}
    </div>
  );
}

function ChartCard({ title, subtitle, children }: { title: string; subtitle: string; children: (table: boolean) => React.ReactNode }) {
  const [table, setTable] = useState(false);
  return (
    <section className="card metrics-card">
      <div className="chart-head">
        <div>
          <h2>{title}</h2>
          <p className="muted small">{subtitle}</p>
        </div>
        <button className="ghost small" onClick={() => setTable((t) => !t)} aria-pressed={table}>
          {table ? "Chart" : "Table"}
        </button>
      </div>
      {children(table)}
    </section>
  );
}

function Tooltip({ text, x }: { text: string; x?: string }) {
  return (
    <div className="viz-tip" role="tooltip" style={x ? { left: x } : undefined}>
      {text}
    </div>
  );
}

function Funnel({ rows }: { rows: Metrics["funnel"] }) {
  const [hover, setHover] = useState<number | null>(null);
  const top = Math.max(1, rows[0]?.count ?? 1);
  if (!rows[0]?.count) return <p className="muted small">No conversations in this range yet.</p>;
  return (
    <div className="funnel" onMouseLeave={() => setHover(null)}>
      {rows.map((r, i) => (
        <div
          key={r.step}
          className={`funnel-row ${hover === i ? "hot" : ""}`}
          onMouseEnter={() => setHover(i)}
          onFocus={() => setHover(i)}
          tabIndex={0}
        >
          <span className="funnel-label">{r.step}</span>
          <span className="funnel-track">
            <span className="funnel-bar" style={{ width: `${(100 * r.count) / top}%` }} />
            <span className="funnel-value">
              {r.count.toLocaleString()}
              {r.drop_pct != null && r.drop_pct > 0 && <span className="muted"> · −{r.drop_pct}%</span>}
            </span>
          </span>
          {hover === i && (
            <Tooltip
              text={`${r.step}: ${r.count.toLocaleString()} (${Math.round((100 * r.count) / top)}% of everyone who opened it${
                r.drop_pct != null ? `; ${r.drop_pct}% dropped off at this step` : ""
              })`}
            />
          )}
        </div>
      ))}
    </div>
  );
}

function FunnelTable({ rows }: { rows: Metrics["funnel"] }) {
  return (
    <table className="mtable">
      <thead><tr><th>Step</th><th>Conversations</th><th>Drop-off from previous</th></tr></thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.step}><td>{r.step}</td><td>{r.count.toLocaleString()}</td><td>{r.drop_pct == null ? "–" : `${r.drop_pct}%`}</td></tr>
        ))}
      </tbody>
    </table>
  );
}

/** Where a latency value falls along the bins, as a % of the plot width (bins are equal-width columns). */
function binPosition(bins: Metrics["voice_latency_hist"], v: number): number {
  const i = bins.findIndex((b) => b.to == null || v < b.to);
  const b = bins[i];
  const frac = b.to == null ? 0.5 : (v - b.from) / (b.to - b.from);
  return ((i + Math.min(1, Math.max(0, frac))) / bins.length) * 100;
}

function Histogram({ bins, p50, p90 }: { bins: Metrics["voice_latency_hist"]; p50: number | null; p90: number | null }) {
  const [hover, setHover] = useState<number | null>(null);
  const max = Math.max(1, ...bins.map((b) => b.count));
  if (!bins.some((b) => b.count)) return <p className="muted small">No voice turns in this range yet.</p>;
  return (
    <div className="hist">
      <div className="hist-plot" onMouseLeave={() => setHover(null)}>
        {[p50, p90].map((v, n) =>
          v == null ? null : (
            <div key={n} className="ref-line" style={{ left: `${binPosition(bins, v)}%` }}>
              <span>{n === 0 ? `median ${ms(v)}` : `p90 ${ms(v)}`}</span>
            </div>
          ),
        )}
        {bins.map((b, i) => (
          <div key={i} className="hist-col" onMouseEnter={() => setHover(i)} tabIndex={0} onFocus={() => setHover(i)}>
            <div className="hist-bar" style={{ height: `${(100 * b.count) / max}%` }} />
            {hover === i && <Tooltip text={`${binLabel(b)} ms: ${b.count} turn${b.count === 1 ? "" : "s"}`} />}
          </div>
        ))}
      </div>
      <div className="hist-axis">
        {bins.map((b, i) => (
          <span key={i}>{binLabel(b)}</span>
        ))}
      </div>
      <p className="hist-unit">milliseconds</p>
    </div>
  );
}

function HistTable({ bins }: { bins: Metrics["voice_latency_hist"] }) {
  return (
    <table className="mtable">
      <thead><tr><th>End of turn to first sound (ms)</th><th>Turns</th></tr></thead>
      <tbody>
        {bins.map((b, i) => (<tr key={i}><td>{binLabel(b)}</td><td>{b.count}</td></tr>))}
      </tbody>
    </table>
  );
}
