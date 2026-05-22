// Minimal ESLint config so Vercel's framework checks pass.
// We don't actually rely on ESLint for our build (`tsc && vite build` skips it).
module.exports = {
  root: true,
  env: { browser: true, es2020: true, node: true },
  parser: '@typescript-eslint/parser',
  parserOptions: { ecmaVersion: 2020, sourceType: 'module', ecmaFeatures: { jsx: true } },
  plugins: ['@typescript-eslint', 'react-hooks', 'react-refresh'],
  rules: {},
  ignorePatterns: ['dist', '.eslintrc.cjs', 'node_modules'],
}
