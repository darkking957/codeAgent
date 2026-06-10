import { useState } from "react";

import { SidebarHeader } from "./SidebarHeader";
import { PrimaryNav } from "./PrimaryNav";
import { SectionLabel } from "./SectionLabel";
import { ProductNav } from "./ProductNav";
import { RecentsList } from "./RecentsList";
import { UserMenu } from "./UserMenu";

/** 左侧栏：字标 + 主导航 + Products + Recents（可滚动）+ 底部用户档案（reference §6）。 */
export function Sidebar() {
  const [query, setQuery] = useState("");
  const [searchOpen, setSearchOpen] = useState(false);

  return (
    <nav className="sidebar" aria-label="主导航">
      <SidebarHeader
        searchOpen={searchOpen}
        query={query}
        onToggleSearch={() => setSearchOpen((v) => !v)}
        onQueryChange={setQuery}
      />
      <div className="sidebar__scroll scroll-area">
        <PrimaryNav />
        <SectionLabel>Products</SectionLabel>
        <ProductNav />
        <SectionLabel>Recents</SectionLabel>
        <RecentsList query={query} />
      </div>
      <UserMenu />
    </nav>
  );
}
