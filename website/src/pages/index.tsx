import type {ReactNode} from 'react';
import Link from '@docusaurus/Link';
import Layout from '@theme/Layout';
import Heading from '@theme/Heading';
import {Check, X} from 'lucide-react';

import styles from './index.module.css';

const lifecycle = [
  ['/review requested', 'A permitted user comments /review on a pull request.'],
  ['Commits pinned', 'The base and head commits are checked and recorded.'],
  ['Code reviewed', 'The model reads the change and records findings; it cannot write to GitHub.'],
  ['Review published', 'A separate publisher rechecks the commits and posts the findings.'],
] as const;

const setupSteps = [
  ['Register the GitHub App', 'The deployment needs its ID, webhook secret, and key at startup.'],
  ['Deploy the service', 'Compose, Dokploy, Coolify, Portainer, or Kubernetes (Helm).'],
  [
    'Install and approve the App',
    'Then enable each repository, or let its first command enable it.',
  ],
  ['Run /review', 'Comment on an open pull request from the same repository.'],
] as const;

const tasks = [
  {
    name: 'Reviewing a pull request',
    description: 'Who can ask, what to comment, and what gets published.',
    label: 'Request a review',
    to: '/docs/getting-started#4-request-a-review',
  },
  {
    name: 'Maintaining a repository',
    description: 'Give the reviewer instructions and record design decisions.',
    label: 'Repository context',
    to: '/docs/repository-context',
  },
  {
    name: 'Operating the platform',
    description: 'Deploy, configure, observe, recover, back up, and update the service.',
    label: 'Operations',
    to: '/docs/operations',
  },
  {
    name: 'Assessing security or architecture',
    description: 'Inspect trust boundaries, data handling, and current capabilities.',
    label: 'Security model',
    to: '/docs/security',
  },
] as const;

function Home(): ReactNode {
  return (
    <Layout
      title="Evidence-backed pull-request review"
      description="Documentation for Review Agent: onboarding, configuration, security, operations, and capabilities."
    >
      <main className={styles.main}>
        <section aria-labelledby="home-title">
          <div className={styles.container}>
            <div className={styles.hero}>
              <div>
                <Heading as="h1" id="home-title" className={styles.title}>
                  Review pull requests against the exact code that changed.
                </Heading>
                <p className={styles.summary}>
                  Review Agent is a self-hosted advisory reviewer for GitHub. It
                  pins the base and head commits, analyzes the diff through
                  bounded read tools, and publishes evidence-backed findings
                  without giving the model GitHub write access.
                </p>
                <div className={styles.actions}>
                  <Link className={styles.primaryAction} to="/docs/getting-started">
                    Run the first review
                  </Link>
                  <Link className={styles.secondaryAction} to="/docs/example-review">
                    See an example review
                  </Link>
                </div>
              </div>
              <aside className={styles.artifact} aria-label="Sanitized example review">
                <p className={styles.artifactHead}>Example review</p>
                <div className={styles.artifactMeta}>
                  <span>Pull request #123</span>
                  <code>/review</code>
                </div>
                <div className={styles.artifactMeta}>
                  <span>
                    head <code>a1b2c3d</code>
                  </span>
                  <span className={styles.severity}>Medium (P2)</span>
                </div>
                <div className={styles.artifactBody}>
                  <p className={styles.artifactTitle}>
                    F1 · Retry delay uses milliseconds as seconds
                  </p>
                  <p className={styles.artifactPath}>
                    <code>src/jobs/retry.py:87</code> · correctness
                  </p>
                  <p className={styles.artifactText}>
                    The setting is documented in milliseconds, but the changed
                    scheduler call passes it to an API that waits in seconds.
                  </p>
                </div>
                <p className={styles.artifactStatus}>Published</p>
              </aside>
            </div>
          </div>
        </section>

        <section aria-labelledby="lifecycle-title">
          <div className={styles.container}>
            <Heading as="h2" id="lifecycle-title" className={styles.sectionTitle}>
              From /review to published findings
            </Heading>
            <ol className={styles.lifecycle}>
              {lifecycle.map(([name, description]) => (
                <li key={name}>
                  <span>{name}</span>
                  <small>{description}</small>
                </li>
              ))}
            </ol>
            <p className={styles.lifecycleReturn}>
              To review a fix, push it and comment /review again.
            </p>
          </div>
        </section>

        <section className={styles.band} aria-labelledby="setup-title">
          <div className={styles.container}>
            <Heading as="h2" id="setup-title" className={styles.sectionTitle}>
              Set up the reviewer
            </Heading>
            <div className={styles.setup}>
              <div>
                <p className={styles.setupNote}>
                  One deployment serves every repository where the App is
                  installed and approved. Repositories need no workflow,
                  secret, or local setup.
                </p>
                <p className={styles.setupLinks}>
                  <Link to="/docs/getting-started">Getting started</Link>
                  <Link to="/docs/deployment">Deployment guide</Link>
                </p>
              </div>
              <ol className={styles.setupSteps}>
                {setupSteps.map(([name, description]) => (
                  <li key={name}>
                    <span>{name}</span>
                    <small>{description}</small>
                  </li>
                ))}
              </ol>
            </div>
          </div>
        </section>

        <section aria-labelledby="tasks-title">
          <div className={styles.container}>
            <Heading as="h2" id="tasks-title" className={styles.sectionTitle}>
              Find the guide for your task
            </Heading>
            <div className={styles.taskList}>
              {tasks.map((task) => (
                <div className={styles.task} key={task.name}>
                  <span className={styles.taskName}>{task.name}</span>
                  <p className={styles.taskDescription}>{task.description}</p>
                  <Link to={task.to}>{task.label}</Link>
                </div>
              ))}
            </div>
          </div>
        </section>

        <section className={styles.band} aria-labelledby="boundaries-title">
          <div className={styles.container}>
            <Heading as="h2" id="boundaries-title" className={styles.sectionTitle}>
              Operating boundaries
            </Heading>
            <p className={styles.boundariesLead}>
              The model proposes findings. Application code owns identity,
              state, authorization, and publication.
            </p>
            <div className={styles.boundaries}>
              <div className={styles.does}>
                <Heading as="h3">The reviewer does</Heading>
                <ul>
                  <li>
                    <Check size={16} strokeWidth={2.5} aria-hidden="true" />
                    <span>Review one exact base-to-head snapshot.</span>
                  </li>
                  <li>
                    <Check size={16} strokeWidth={2.5} aria-hidden="true" />
                    <span>
                      Preserve findings, coverage, publication state, and human
                      feedback.
                    </span>
                  </li>
                  <li>
                    <Check size={16} strokeWidth={2.5} aria-hidden="true" />
                    <span>Publish through deterministic application code.</span>
                  </li>
                </ul>
              </div>
              <div className={styles.doesNot}>
                <Heading as="h3">The reviewer does not</Heading>
                <ul>
                  <li>
                    <X size={16} strokeWidth={2.5} aria-hidden="true" />
                    <span>Execute contributor code through a general shell.</span>
                  </li>
                  <li>
                    <X size={16} strokeWidth={2.5} aria-hidden="true" />
                    <span>Treat pull-request text as trusted instructions.</span>
                  </li>
                  <li>
                    <X size={16} strokeWidth={2.5} aria-hidden="true" />
                    <span>Receive arbitrary GitHub write access.</span>
                  </li>
                </ul>
              </div>
            </div>
          </div>
        </section>
      </main>
    </Layout>
  );
}

export default Home;
