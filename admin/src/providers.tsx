import { ScopedLink as Link } from "./scope";

export function Providers() {
  return (
    <section className="section">
      <div className="section-heading">
        <div>
          <h2>Model connections</h2>
          <p>
            Manage shared and team-owned provider accounts, sign-ins, and
            allowed models.
          </p>
        </div>
      </div>
      <Link className="button secondary" to="/model-connections">
        Open model connections
      </Link>
    </section>
  );
}
