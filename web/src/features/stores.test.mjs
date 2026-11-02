import assert from "node:assert/strict";
import test from "node:test";
import {
  resetOperatorSessionUiState,
  useSessionStore,
  useWarehouseStore,
  useWordsStore,
  usePublishLogsStore,
} from "./stores.ts";

test("operator session changes reset account-scoped UI state", () => {
  useSessionStore.getState().setCurrentShopId("shop-previous-user");
  useWarehouseStore.setState({
    page: 4,
    pageSize: 50,
    sort: "priceAsc",
    searchDraft: "warehouse search",
    q: "warehouse query",
    claimed: true,
    status: "published",
    qualityStatus: "needs_review",
    category: { kind: "group", groupId: "group-1", name: "Group 1" },
    selected: ["product-1", "product-2"],
  });
  useWordsStore.setState({
    page: 3,
    pageSize: 200,
    category: "remove",
    searchDraft: "word search",
    q: "word query",
    selectedWords: ["word-1", "word-2"],
  });
  usePublishLogsStore.setState({ page: 5, pageSize: 50, status: "failed" });

  resetOperatorSessionUiState();

  assert.equal(useSessionStore.getState().currentShopId, null);
  assert.deepEqual(
    {
      page: useWarehouseStore.getState().page,
      pageSize: useWarehouseStore.getState().pageSize,
      sort: useWarehouseStore.getState().sort,
      searchDraft: useWarehouseStore.getState().searchDraft,
      q: useWarehouseStore.getState().q,
      claimed: useWarehouseStore.getState().claimed,
      status: useWarehouseStore.getState().status,
      qualityStatus: useWarehouseStore.getState().qualityStatus,
      category: useWarehouseStore.getState().category,
      selected: useWarehouseStore.getState().selected,
    },
    {
      page: 1,
      pageSize: 20,
      sort: "savedDesc",
      searchDraft: "",
      q: "",
      claimed: null,
      status: null,
      qualityStatus: null,
      category: { kind: "all" },
      selected: [],
    },
  );
  assert.deepEqual(
    {
      page: useWordsStore.getState().page,
      pageSize: useWordsStore.getState().pageSize,
      category: useWordsStore.getState().category,
      searchDraft: useWordsStore.getState().searchDraft,
      q: useWordsStore.getState().q,
      selectedWords: useWordsStore.getState().selectedWords,
    },
    { page: 1, pageSize: 100, category: null, searchDraft: "", q: "", selectedWords: [] },
  );
  assert.deepEqual(
    {
      page: usePublishLogsStore.getState().page,
      pageSize: usePublishLogsStore.getState().pageSize,
      status: usePublishLogsStore.getState().status,
    },
    { page: 1, pageSize: 20, status: null },
  );
});
