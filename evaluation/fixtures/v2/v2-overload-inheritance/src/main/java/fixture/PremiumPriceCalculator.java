package fixture;

public class PremiumPriceCalculator extends BasePriceCalculator {
    @Override
    public int price(int base) {
        return base + 10;
    }
}
