import type {SidebarsConfig} from '@docusaurus/plugin-content-docs';

const sidebars: SidebarsConfig = {
  documentation: [
    {
      type: 'category',
      label: 'Start here',
      collapsed: false,
      items: [
        'docs/OVERVIEW',
        'docs/GETTING_STARTED',
        'docs/HOW_REVIEWS_WORK',
        'examples/comments/example-review',
      ],
    },
    {
      type: 'category',
      label: 'Set up',
      collapsed: false,
      items: [
        'docs/DEPLOYMENT',
        'docs/GITHUB_APP_PILOT',
        'docs/AI_ASSISTED_SETUP',
        'docs/BEHAVIOR_OWNERSHIP',
      ],
    },
    {
      type: 'category',
      label: 'Operate',
      collapsed: false,
      items: [
        'docs/OPERATIONS',
        'docs/FEEDBACK_AND_DECISIONS',
        'docs/SECURITY',
        'docs/FAQ',
      ],
    },
    {
      type: 'category',
      label: 'Reference',
      collapsed: false,
      items: ['docs/ROADMAP'],
    },
  ],
};

export default sidebars;
