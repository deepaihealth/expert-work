/**
 * Section breadcrumb of a knowledge chunk — B-175 L2-PR3.
 *
 * The backend stores a chunk's body clean and its heading path apart
 * (``heading_path``, outermost first). Chunks ingested before that still carry
 * the ``[Section: …]`` prefix inside their body and an empty path, so this
 * renders nothing for them.
 */
import { Typography } from "antd";

const { Text } = Typography;

interface HeadingPathProps {
  path: readonly string[] | undefined;
  testId?: string;
}

export function HeadingPath({ path, testId }: HeadingPathProps) {
  if (!path || path.length === 0) return null;
  return (
    <Text
      type="secondary"
      style={{ display: "block", fontSize: 12, marginBottom: 4 }}
      data-testid={testId}
    >
      {path.join(" › ")}
    </Text>
  );
}
