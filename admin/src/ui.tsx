export const number = new Intl.NumberFormat();

/** A single figure in a summary grid. Counts are derived from the rows on the
 *  current page, so callers state what the figures cover alongside the grid. */
export function Stat({
  label,
  value,
  attention,
}: {
  label: string;
  value: number;
  attention?: boolean;
}) {
  return (
    <div className="stat">
      <span className="stat-label">{label}</span>
      <span
        className={attention && value ? "stat-value attention" : "stat-value"}
      >
        {number.format(value)}
      </span>
    </div>
  );
}
