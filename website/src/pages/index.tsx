import type {ReactNode} from 'react';
import Link from '@docusaurus/Link';
import Layout from '@theme/Layout';
import Heading from '@theme/Heading';
import Tabs from '@theme/Tabs';
import TabItem from '@theme/TabItem';
import CodeBlock from '@theme/CodeBlock';
import {Check, X} from 'lucide-react';

import styles from './index.module.css';

const lifecycle = [
  [
    '/review requested',
    'An authorized maintainer asks for a review with a pull-request comment.',
  ],
  ['Snapshot locked', 'The exact base and head commits are pinned and recorded.'],
  ['Diff analyzed', 'Bounded read tools feed a two-pass, evidence-first review.'],
  [
    'Review published',
    'Stored findings reach GitHub through a recoverable publisher.',
  ],
] as const;

const setupSteps = [
  ['Deploy the service', 'Start the reviewer stack and PostgreSQL from compose.yaml.'],
  [
    'Connect GitHub',
    'Install the GitHub App on selected repositories, then enable each approved repository.',
  ],
  [
    'Run /review',
    'Comment on an open pull request; a changed snapshot starts a new round.',
  ],
] as const;

const tasks = [
  {
    name: 'Reviewing a pull request',
    description: 'Understand checks, evidence, publication, and feedback.',
    label: 'How reviews work',
    to: '/docs/how-reviews-work',
  },
  {
    name: 'Maintaining a repository',
    description: 'Add a repository and find who owns policy and runtime wiring.',
    label: 'Behavior ownership',
    to: '/docs/behavior-ownership',
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
            <p className={styles.lifecycleReturn}>Human feedback starts a new round.</p>
          </div>
        </section>

        <section className={styles.band} aria-labelledby="setup-title">
          <div className={styles.container}>
            <Heading as="h2" id="setup-title" className={styles.sectionTitle}>
              Set up the reviewer
            </Heading>
            <div className={styles.setup}>
              <div className={styles.setupDeploy}>
                <Tabs groupId="deployment-platform">
                  <TabItem value="compose" label="Compose / Dokploy" default>
                    <CodeBlock language="bash">
                      {'docker compose config --quiet\ndocker compose up -d --build\ndocker compose ps'}
                    </CodeBlock>
                  </TabItem>
                  <TabItem value="coolify-portainer" label="Coolify / Portainer">
                    <p className={styles.setupNote}>
                      Import <code>compose.yaml</code> as a Compose stack and
                      enter the values from <code>.env.example</code> in the
                      platform secret UI.
                    </p>
                  </TabItem>
                  <TabItem value="openshift" label="OpenShift">
                    <CodeBlock language="bash">
                      {'oc process -f examples/openshift/review-agent-template.yaml \\\n  -p IMAGE="$REVIEW_AGENT_IMAGE" | oc apply -f -'}
                    </CodeBlock>
                  </TabItem>
                </Tabs>
                <p className={styles.setupLinks}>
                  <Link to="/docs/deployment">Open the deployment guide</Link>
                  <Link to="/docs/behavior-ownership">Configure behavior</Link>
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
            <p className={styles.boundariesNote}>
              Signed webhook admission stores durable review jobs in one
              PostgreSQL database per environment, and a transactional
              publication outbox hands immutable comment parts to a recoverable
              publisher.
            </p>
          </div>
        </section>
      </main>
    </Layout>
  );
}

export default Home;
