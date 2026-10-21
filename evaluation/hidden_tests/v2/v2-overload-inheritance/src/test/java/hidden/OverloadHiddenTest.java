package hidden;

import fixture.PriceService;
import org.junit.Assert;
import org.junit.Test;

public class OverloadHiddenTest {
    @Test
    public void doubleOverloadRemainsUnchanged() {
        Assert.assertEquals(100.0, new PriceService().quoteDouble(100), 0.001);
    }
}
