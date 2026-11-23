export type DeleteTarget = { projectId: string; targetId: string; targetKind: "run" | "recording" };

export function confirmProjectDeletion(target: DeleteTarget, activeProject: string,
                                       typedConfirmation: string): DeleteTarget {
  if (!target.projectId || !target.targetId || target.projectId !== activeProject) {
    throw new Error("Destructive operation targets an unauthorized project");
  }
  if (target.targetKind !== "run" && target.targetKind !== "recording") {
    throw new Error("Unsupported deletion target");
  }
  if (typedConfirmation !== `${target.projectId}:${target.targetKind}:${target.targetId}`) {
    throw new Error("Type the exact deletion target to confirm");
  }
  return target;
}
