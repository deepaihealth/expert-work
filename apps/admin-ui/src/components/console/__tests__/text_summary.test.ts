/**
 * text_summary — the console's shared one-line summary helper. Pins the
 * terminator set both projections (Task 10's ``CompactRow`` middle column and
 * Task 16's ``TrajectoryRows`` right rail) now share: full-width 。/！/？ and
 * half-width !/? and newline cut; a bare half-width `.` does NOT (it would
 * truncate a decimal — `置信度 0.8 不够` → `置信度 0`).
 */
import { describe, expect, it } from "vitest";

import { firstSentence } from "../text_summary";

describe("firstSentence", () => {
  it("does not cut on a decimal point", () => {
    expect(firstSentence("置信度 0.8 不够")).toBe("置信度 0.8 不够");
  });

  it("cuts at the first full-width 。and drops the rest", () => {
    expect(firstSentence("档案查完了。再看工单")).toBe("档案查完了");
  });

  it("cuts at half-width ! ? and full-width ！ ？", () => {
    expect(firstSentence("成了!下一步")).toBe("成了");
    expect(firstSentence("行不行?再看看")).toBe("行不行");
    expect(firstSentence("成了！下一步")).toBe("成了");
    expect(firstSentence("行不行？再看看")).toBe("行不行");
  });

  it("cuts at the first newline", () => {
    expect(firstSentence("第一行\n第二行")).toBe("第一行");
  });

  it("trims the cut result and passes an empty string through", () => {
    expect(firstSentence("  两边有空格  。尾巴")).toBe("两边有空格");
    expect(firstSentence("")).toBe("");
  });
});
