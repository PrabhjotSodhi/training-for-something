import { Area, ComposedChart, Line, ReferenceDot, Tooltip, XAxis, YAxis } from "recharts";
import { ChartFrame, day, gutter, MARGIN } from "./ChartFrame";

const DAY = 86_400_000;

export type LinePoint = { date: string; value: number | null; low?: number | null; high?: number | null };

type LineChartProps = {
  points: LinePoint[];
  /** The left and right edge dates. The right edge is labelled Today. */
  start: string;
  end: string;
  /** Readings up to this many days apart are joined by a line. */
  joinDays: number;
  /** The reading's name in the hover, like "Resting HR". */
  name: string;
  format: (value: number) => string;
  unit?: string;
};

const time = (date: string) => Date.parse(`${date}T00:00`);

/** The line every card uses: an optional normal band, goal colour outside it and a halo on the latest reading. */
export function LineChart({ points, start, end, joinDays, name, format, unit }: LineChartProps) {
  // Same-day readings get a 1 ms nudge so each keeps its own point on the time axis.
  const rows = points.map((point, index) => ({
    ...point,
    index,
    x: time(point.date) + index,
    band: point.low == null || point.high == null ? null : [point.low, point.high],
  }));
  const readings = rows.filter((row) => row.value !== null);
  const segments: number[] = [];
  let group = -1;
  readings.forEach((row, position) => {
    const gap = position && (time(row.date) - time(readings[position - 1].date)) / DAY;
    if (!position || gap > joinDays) group += 1;
    segments[row.index] = group;
  });
  const groups = Array.from({ length: group + 1 }, (_, number) => number);
  const withSegments = rows.map((row) => {
    const own = segments[row.index];
    if (own === undefined) return row;
    const near = [rows[row.index - 1], row, rows[row.index + 1]].filter(
      (other) => other && segments[other.index] === own,
    );
    const outside = near.some(
      (other) => other.low != null && (other.value! < other.low || other.value! > other.high!),
    );
    return { ...row, [`in${own}`]: row.value, [`out${own}`]: outside ? row.value : null };
  });

  const last = readings.at(-1);
  const banded = readings.filter((row) => row.low != null).at(-1);
  const values = readings.flatMap((row) => [row.value!, ...(row.low == null ? [] : [row.low, row.high!])]);
  const span = Math.max(...values) - Math.min(...values) || 1;
  const bottom = Math.min(...values) - span * 0.35;
  const top = Math.max(...values) + span * 0.35;
  const labels = !last
    ? []
    : banded
      ? gutter(banded.high!, banded.low!, bottom, top, format)
      : gutter(Math.max(...values), Math.min(...values), bottom, top, format);
  const hasDot = (index: number) =>
    joinDays > 1 || (segments[index - 1] !== segments[index] && segments[index + 1] !== segments[index]);
  const withUnit = (value: number) => (unit ? `${format(value)} ${unit}` : format(value));

  return (
    <ChartFrame start={start} labels={labels}>
      <ComposedChart data={withSegments} margin={{ top: MARGIN, right: 0, bottom: MARGIN, left: 0 }}>
        <XAxis dataKey="x" type="number" domain={[time(start), time(end) + rows.length]} hide />
        <YAxis domain={[bottom, top]} hide />
        <Area
          dataKey="band"
          type="monotone"
          stroke="none"
          fill="var(--band)"
          fillOpacity={1}
          isAnimationActive={false}
          activeDot={false}
        />
        <Tooltip
          isAnimationActive={false}
          cursor={{ stroke: "var(--faint)", strokeWidth: 1 }}
          content={({ active, payload }) => {
            const row = payload?.[0]?.payload as (typeof rows)[number] | undefined;
            if (!active || !row) return null;
            return (
              <div className="hover" role="tooltip">
                <b>{day(row.date)}</b>
                <span>
                  {name}
                  <i>{row.value === null ? "No data" : withUnit(row.value)}</i>
                </span>
                {row.low != null && (
                  <>
                    <span>
                      Low<i>{format(row.low)}</i>
                    </span>
                    <span>
                      High<i>{format(row.high!)}</i>
                    </span>
                  </>
                )}
              </div>
            );
          }}
        />
        {groups.map((each) => (
          <Line
            key={`in${each}`}
            dataKey={`in${each}`}
            type="monotone"
            stroke="var(--ink)"
            strokeWidth={2.25}
            strokeLinecap="round"
            connectNulls
            isAnimationActive={false}
            dot={(dot) =>
              dot.value != null && hasDot(dot.index) ? (
                <circle key={dot.index} cx={dot.cx} cy={dot.cy} r={3} className="chart-dot" />
              ) : (
                <g key={dot.index} />
              )
            }
            activeDot={{ r: 4, fill: "var(--ink)", stroke: "var(--card)", strokeWidth: 2 }}
          />
        ))}
        {groups.map((each) => (
          <Line
            key={`out${each}`}
            dataKey={`out${each}`}
            type="monotone"
            stroke="var(--goal)"
            strokeWidth={2.75}
            strokeLinecap="round"
            isAnimationActive={false}
            dot={false}
            activeDot={false}
          />
        ))}
        {last && (
          <ReferenceDot
            x={last.x}
            y={last.value!}
            ifOverflow="visible"
            shape={({ cx, cy }: { cx?: number; cy?: number }) => (
              <g>
                <circle cx={cx} cy={cy} r={11} className="chart-halo" />
                <circle cx={cx} cy={cy} r={5.5} className="chart-today" />
              </g>
            )}
          />
        )}
      </ComposedChart>
    </ChartFrame>
  );
}
