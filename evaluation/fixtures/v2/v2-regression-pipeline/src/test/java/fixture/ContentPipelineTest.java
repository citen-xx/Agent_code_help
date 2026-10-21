package fixture;

import static org.junit.Assert.assertEquals;
import org.junit.Test;

public class ContentPipelineTest {
    private final ContentPipeline pipeline = new ContentPipeline();

    @Test
    public void transformsSlugThenPreservesDisplayCase() {
        assertEquals("hello-world", pipeline.slug(" Hello World "));
        assertEquals("OrderId", pipeline.display("OrderId"));
    }
}
