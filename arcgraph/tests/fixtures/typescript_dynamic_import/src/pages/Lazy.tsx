export function LazyRoute() {
  import("../features/Dashboard");
  import(`../features/TemplateOnly`);
  import("@/features/Widget");
  import("react");
  import("@/missing/Ghost");

  const dynamicPath = "../features/Dashboard";
  import(dynamicPath);
  import(`../features/${dynamicPath}`);

  return null;
}
