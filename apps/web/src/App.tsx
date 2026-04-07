import WorkbenchPage from "./pages/WorkbenchPage";
import { useWorkbenchState } from "./hooks/useWorkbenchState";

export default function App() {
  const state = useWorkbenchState();
  return <WorkbenchPage state={state} />;
}
