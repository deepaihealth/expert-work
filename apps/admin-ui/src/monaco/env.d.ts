// Vite ``?worker`` imports (tsconfig only loads @types/node, not vite/client).
declare module "*?worker" {
  const WorkerCtor: new () => Worker;
  export default WorkerCtor;
}
