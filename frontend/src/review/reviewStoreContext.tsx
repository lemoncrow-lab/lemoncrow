import { createContext, useContext, useReducer, type Dispatch, type ReactNode } from "react";

import {
  createReviewStoreState,
  reviewStoreReducer,
  type ReviewStoreAction,
  type ReviewStoreState,
} from "./reviewStore";

const StateContext = createContext<ReviewStoreState | null>(null);
const DispatchContext = createContext<Dispatch<ReviewStoreAction> | null>(null);

/** Holds one review's state for as long as the Reader is mounted; nothing resets it. */
export function ReviewStoreProvider({ children }: { children: ReactNode }) {
  const [state, dispatch] = useReducer(reviewStoreReducer, undefined, createReviewStoreState);
  return (
    <DispatchContext.Provider value={dispatch}>
      <StateContext.Provider value={state}>{children}</StateContext.Provider>
    </DispatchContext.Provider>
  );
}

export function useReviewStoreState(): ReviewStoreState {
  const state = useContext(StateContext);
  if (!state) throw new Error("useReviewStoreState needs a ReviewStoreProvider above it");
  return state;
}

export function useReviewStoreDispatch(): Dispatch<ReviewStoreAction> {
  const dispatch = useContext(DispatchContext);
  if (!dispatch) throw new Error("useReviewStoreDispatch needs a ReviewStoreProvider above it");
  return dispatch;
}
