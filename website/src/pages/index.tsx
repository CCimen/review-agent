import type {ReactNode} from 'react';
import clsx from 'clsx';
import Link from '@docusaurus/Link';
import Layout from '@theme/Layout';
import Heading from '@theme/Heading';

import styles from './index.module.css';

const lifecycle = [
  ['Request', 'Trusted developer and signed webhook'],
  ['Lock snapshot', 'Exact base and head identity'],
  ['Analyze', 'Bounded reads and skeptical challenge'],
  ['Publish', 'Recheck and deterministic delivery'],
  ['Improve', 'Human feedback and a fresh round'],
] as const;

const entryPoints = [
  {
    title: 'I am reviewing a pull request',
    description:
      'Understand what the reviewer checks, which evidence it uses, and how it publishes results.',
    to: '/docs/how-reviews-work',
  },
  {
    title: 'I maintain a repository',
    description:
      'Add a repository and find the owner for identity, rules, procedure, and runtime wiring.',
    to: '/docs/behavior-ownership',
  },
  {
    title: 'I operate the platform',
    description:
      'Deploy, configure, observe, recover, back up, and update the shared service.',
    to: '/docs/operations',
  },
  {
    title: 'I assess security or architecture',
    description:
      'Inspect trust boundaries, data handling, current limitations, and planned platform work.',
    to: '/docs/security',
  },
] as const;

function Home(): ReactNode {
  return (
    <Layout
      title="Evidence-backed pull-request review"
      description="Documentation for the Sundsvall Review Agent: onboarding, behavior ownership, trust, operations, and roadmap."
    >
      <main className={styles.main}>
        <section className={styles.intro} aria-labelledby="home-title">
          <div className={styles.introCopy}>
            <p className={styles.status}>Advisory · Pilot</p>
            <Heading as="h1" id="home-title" className={styles.title}>
              Review pull requests with evidence, not guesswork.
            </Heading>
            <p className={styles.summary}>
              Sundsvall Review Agent combines bounded model reasoning with
              deterministic authorization, snapshot checks, durable memory, and
              GitHub publication.
            </p>
            <div className={styles.actions}>
              <Link className={styles.primaryAction} to="/docs/getting-started">
                Run the first review
              </Link>
              <Link className={styles.secondaryAction} to="/docs/how-reviews-work">
                See how reviews work
              </Link>
            </div>
          </div>
          <aside className={styles.trustNote} aria-label="Current deployment status">
            <strong>Available now</strong>
            <p>
              One shared, advisory reviewer for an explicit repository allowlist.
              It reads no contributor code through a shell and publishes only
              through narrow deterministic gateways.
            </p>
          </aside>
        </section>

        <section className={styles.lifecycleSection} aria-labelledby="lifecycle-title">
          <Heading as="h2" id="lifecycle-title">
            One traceable review path
          </Heading>
          <ol className={styles.lifecycle}>
            {lifecycle.map(([name, description]) => (
              <li key={name}>
                <span>{name}</span>
                <small>{description}</small>
              </li>
            ))}
          </ol>
        </section>

        <section className={styles.entrySection} aria-labelledby="entry-title">
          <Heading as="h2" id="entry-title">
            Start with the task in front of you
          </Heading>
          <div className={styles.entryList}>
            {entryPoints.map((entry) => (
              <Link className={styles.entry} to={entry.to} key={entry.title}>
                <span>{entry.title}</span>
                <p>{entry.description}</p>
              </Link>
            ))}
          </div>
        </section>

        <section className={styles.boundariesSection} aria-label="Product boundaries">
          <div>
            <Heading as="h2">What it does</Heading>
            <ul>
              <li>Reviews one exact base-to-head snapshot.</li>
              <li>Challenges actionable correctness, security, contract, and maintainability risks.</li>
              <li>Preserves findings, coverage, publication state, and human feedback.</li>
              <li>Publishes through deterministic application code.</li>
            </ul>
          </div>
          <div>
            <Heading as="h2">What it does not do</Heading>
            <ul>
              <li>Execute contributor code through a general shell.</li>
              <li>Treat pull-request text as trusted instructions.</li>
              <li>Replace deterministic CI or human merge ownership.</li>
              <li>Give the model arbitrary GitHub write access.</li>
            </ul>
          </div>
        </section>

        <section className={clsx(styles.statusSection, styles.divided)} aria-labelledby="status-title">
          <div>
            <Heading as="h2" id="status-title">
              Clear about what is current
            </Heading>
            <p>
              Today the platform uses protected GitHub Actions workflows, HMAC
              webhooks, repository-scoped tokens, Docker/Dokploy, and SQLite.
            </p>
          </div>
          <div>
            <Heading as="h2">Deliberate about what comes next</Heading>
            <p>
              The initial PostgreSQL schema and real-engine CI contract are in
              place, while the active reviewer still uses SQLite. The remaining
              milestones add the runtime owner, cut over one PostgreSQL database
              per environment, delete SQLite application persistence, and then
              add durable jobs and an outbox.
            </p>
            <Link to="/docs/roadmap">Read the current and planned capabilities</Link>
          </div>
        </section>
      </main>
    </Layout>
  );
}

export default Home;
