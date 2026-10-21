package hidden;

import fixture.ContentPipeline;
import org.junit.Assert;
import org.junit.Test;

public class PipelineRegressionHiddenTest {
    @Test
    public void displayTrimsOuterWhitespace() {
        Assert.assertEquals("Hello", new ContentPipeline().display("  Hello  "));
    }
}
