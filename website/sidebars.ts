import type {SidebarsConfig} from '@docusaurus/plugin-content-docs';

const sidebars: SidebarsConfig = {
  documentation: [
    {
      type: 'category',
      label: 'Start here',
      collapsed: false,
      items: ['docs/GETTING_STARTED', 'docs/HOW_REVIEWS_WORK'],
    },
    {
      type: 'category',
      label: 'Understand',
      collapsed: false,
      items: [
        'README',
        'docs/BEHAVIOR_OWNERSHIP',
        'examples/comments/example-review',
      ],
    },
    {
      type: 'category',
      label: 'Operate',
      collapsed: false,
      items: ['docs/OPERATIONS', 'docs/SECURITY', 'docs/FAQ'],
    },
    {
      type: 'category',
      label: 'Future',
      collapsed: false,
      items: ['docs/ROADMAP'],
    },
  ],
};

export default sidebars;
