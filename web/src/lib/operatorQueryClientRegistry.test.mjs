import assert from "node:assert/strict";
import test from "node:test";
import { QueryClient } from "@tanstack/react-query";
import {
  clearOperatorQueryClients,
  clearOperatorQueryClientsExcept,
  getOperatorQueryClient,
} from "./operatorQueryClientRegistry.mjs";

test("switching directly between operators reuses isolated query caches", () => {
  const root = new QueryClient({ defaultOptions: { queries: { staleTime: 1234 } } });
  const operatorA = getOperatorQueryClient("operator-a:member", root);
  operatorA.setQueryData(["warehouse"], { owner: "operator-a" });

  const operatorB = getOperatorQueryClient("operator-b:member", root);
  assert.notEqual(operatorB, operatorA);
  assert.equal(operatorB.getQueryData(["warehouse"]), undefined);
  assert.equal(operatorB.getDefaultOptions().queries?.staleTime, 1234);

  operatorB.setQueryData(["warehouse"], { owner: "operator-b" });
  assert.equal(getOperatorQueryClient("operator-a:member", root), operatorA);
  assert.deepEqual(operatorA.getQueryData(["warehouse"]), { owner: "operator-a" });
  assert.deepEqual(operatorB.getQueryData(["warehouse"]), { owner: "operator-b" });

  clearOperatorQueryClients();
});

test("leaving an operator clears its sensitive cache and retains the public cache", () => {
  const root = new QueryClient();
  const operatorA = getOperatorQueryClient("operator-a:member", root);
  operatorA.setQueryData(["publish-logs"], [{ title: "private product" }]);
  const publicClient = getOperatorQueryClient(null, root);
  publicClient.setQueryData(["software-login"], { available: true });

  clearOperatorQueryClientsExcept(publicClient);

  assert.equal(operatorA.getQueryData(["publish-logs"]), undefined);
  assert.deepEqual(publicClient.getQueryData(["software-login"]), { available: true });
  assert.equal(getOperatorQueryClient(null, root), publicClient);

  clearOperatorQueryClients();
});
