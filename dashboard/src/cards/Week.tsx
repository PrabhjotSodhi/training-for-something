import type { components } from "../api";
import "./week.css";

export type WeekProps = NonNullable<components["schemas"]["WeekOverviewCard"]["data"]>;

const sports: Record<string, string> = {
  run: "Run",
  ride: "Ride",
  swim: "Swim",
  strength: "Strength",
};

const number = (value: number) => value.toLocaleString(undefined, { maximumFractionDigits: 1 });
const sportName = (sport: string) => sports[sport] ?? sport[0].toUpperCase() + sport.slice(1);

export function Week({ week }: { week: WeekProps }) {
  return (
    <div className="week">
      <div className="card-title">This week</div>
      <div className="week-days">
        {week.days.map((day) => {
          const when = new Date(`${day.date}T00:00`);
          const weekday = when.toLocaleDateString(undefined, { weekday: "short" });
          return (
            <div key={day.date} className={day.date === week.today ? "week-day now" : "week-day"}>
              <div className="week-label">
                {weekday} <span>{when.getDate()}</span>
              </div>
              {day.sessions.length === 0 && <div className="week-rest">Rest</div>}
              {day.sessions.map((session, index) => {
                const amount = `${number(session.amount)} ${session.unit}`;
                const time = session.minutes ? `${Math.round(session.minutes)} min` : "No time";
                return (
                  <div
                    key={index}
                    className={`week-tile ${session.state}`}
                    style={{ height: Math.max(46, (session.minutes ?? 0) * 1.05) }}
                    tabIndex={0}
                    aria-label={`${weekday} ${when.getDate()}, ${session.name}, ${amount}, ${time}, ${session.state}`}
                  >
                    <div className="week-name">{session.name}</div>
                    <div className="week-amount">{amount}</div>
                    <div className="week-hover" role="tooltip">
                      <b>{session.name}</b>
                      <span>
                        {weekday} {when.getDate()}, {session.state === "done" ? "Done" : "Planned"}
                      </span>
                      <span>{amount}</span>
                      <span>{time}</span>
                    </div>
                  </div>
                );
              })}
            </div>
          );
        })}
      </div>
      <div className="week-totals">
        {week.totals.map((total) => (
          <div key={`${total.sport}-${total.unit}`} className="week-total">
            <div className="week-total-head">
              <span>{sportName(total.sport)}</span>
              <span>
                {number(total.done)} of {number(total.planned)} {total.unit}
              </span>
            </div>
            <div className="week-bar">
              <span
                style={{ width: `${Math.min(100, (total.done / (total.planned || total.done)) * 100)}%` }}
              />
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
