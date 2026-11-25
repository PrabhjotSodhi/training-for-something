import type { components } from "../api";
import "./week.css";

export type WeekProps = NonNullable<components["schemas"]["WeekOverviewCard"]["data"]>;

const sports: Record<string, string> = {
  run: "Run",
  ride: "Ride",
  swim: "Swim",
  strength: "Strength",
};

const states = { done: "Done", planned: "Planned", missed: "Missed" };

const number = (value: number) => value.toLocaleString(undefined, { maximumFractionDigits: 1 });
const sportName = (sport: string) => sports[sport] ?? sport[0].toUpperCase() + sport.slice(1);

export function Week({ week }: { week: WeekProps }) {
  const sessions = week.days.flatMap((day) => day.sessions);
  const done = sessions.filter((session) => session.state === "done").length;
  const planned = week.totals.some((total) => total.planned > 0);
  const count = planned ? sessions.length : done;
  return (
    <div className="week">
      <div className="card-title">This week</div>
      <div className="week-body">
        <div className="week-days">
          {week.days.map((day) => {
            const when = new Date(`${day.date}T00:00`);
            const weekday = when.toLocaleDateString(undefined, { weekday: "short" });
            const now = day.date === week.today;
            return (
              <div key={day.date} className={now ? "week-day now" : "week-day"}>
                <div className="week-label">
                  {weekday} <span>{when.getDate()}</span>
                </div>
                {day.sessions.length === 0 && <div className="week-rest">Rest</div>}
                {day.sessions.map((session, index) => {
                  const amount = `${number(session.amount)} ${session.unit}`;
                  const time = session.minutes ? `${Math.round(session.minutes)} min` : "No time";
                  const look = now && session.state === "planned" ? "today" : session.state;
                  return (
                    <div
                      key={index}
                      className={`week-tile ${session.sport} ${look}`}
                      style={{ height: Math.max(46, (session.minutes ?? 0) * 1.05) }}
                      tabIndex={0}
                      aria-label={`${weekday} ${when.getDate()}, ${session.name}, ${amount}, ${time}, ${session.state}`}
                    >
                      <div className="week-name">{session.name}</div>
                      <div className="week-amount">{session.state === "missed" ? "Missed" : amount}</div>
                      <div className="week-hover hover" role="tooltip">
                        <b>
                          {weekday} {when.getDate()}, {session.name}
                        </b>
                        <span>
                          {states[session.state]}
                          <i>{amount}</i>
                        </span>
                        <span>
                          Time<i>{time}</i>
                        </span>
                      </div>
                    </div>
                  );
                })}
              </div>
            );
          })}
        </div>
        <div className="week-totals">
          <div className="week-count">
            {done}
            {planned && <span> of {sessions.length}</span>}
          </div>
          <div className="card-note">{count === 1 ? "session" : "sessions"}</div>
          {week.totals.map((total) => (
            <div key={`${total.sport}-${total.unit}`} className={`week-total ${total.sport}`}>
              <div className="week-total-head">
                <span>
                  <i className="week-swatch" />
                  {sportName(total.sport)}
                </span>
                <span>
                  <b>{number(total.done)}</b> {total.planned > 0 && `of ${number(total.planned)} `}
                  {total.unit}
                </span>
              </div>
              {total.planned > 0 && (
                <div className="week-bar">
                  <span style={{ width: `${Math.min(100, (total.done / total.planned) * 100)}%` }} />
                </div>
              )}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
